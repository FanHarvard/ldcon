import copy
from types import SimpleNamespace

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.jobs.recovery_reference import infer, select
from ldcon.jobs.recovery_procedure import parameters, execute, run


def settings():
    return dict(lookback_s=30., stable_s=3., min_samples=4, max_gap_s=1.5,
                guard_s=1., max_stddev_hz=3., max_slope_hz_per_s=2.)


def history():
    return [dict(acquired_s=float(i), frequency_hz=1000.+i*.1,
                 operation_epoch='a', outputs=dict(current=.5, temperature=.2, piezo=30000.))
            for i in range(12)]


def config():
    return parameters({'parameters': dict(reference=settings(), fallback=[dict(offset=-.1, dwell_s=0.),
        dict(offset=0., dwell_s=0.)], tolerance_hz=5., hop_threshold_hz=100., stable_s=2.,
        confirm_samples=3,sample_interval_s=.5,max_sample_age_s=2.,measurement_timeout_s=30.,
        timeout_s=600.,execution='recover',execution_mode='deterministic',
        history=history(), event_time_s=12., operation_epoch='a')}, {})


def test_infer_uses_latest_stable_pre_hop_data_not_map():
    h=history()+[dict(history()[-1], acquired_s=12., frequency_hz=5000.)]
    ref=infer(h, 12., 'a', settings())
    assert ref['frequency_hz']==pytest.approx(1000.95)
    assert ref['end_s']==11.
    assert not ref['physical_mode_identity_verified']


def test_epochs_invalid_samples_gaps_and_drift_do_not_manufacture_reference():
    for h in ([dict(r, operation_epoch='b') for r in history()],
              [dict(r, frequency_hz=1000.+30*r['acquired_s']) for r in history()],
              [dict(r, acquired_s=r['acquired_s']*3) for r in history()]):
        with pytest.raises(ValueError, match='No stable'):
            infer(h, 35. if h[-1]['acquired_s']>12 else 12., 'a', settings())
    with pytest.raises(ValueError, match='ordered'):
        infer(history()+[history()[-1]], 12., 'a', settings())


def test_ambiguous_specialization_uses_fallback_not_arbitrary_branch():
    ref=infer(history(),12.,'a',settings())
    route=[dict(offset=-.02,dwell_s=1.)]
    item=dict(id='one',frequency_range_hz=[999.,1002.],route=route,context={'temperature':[.1,.3]})
    assert select(ref,[],[item])[1]['id']=='one'
    assert select(ref,[],[item,dict(item,id='two')])[1]['reason']=='ambiguous'
    assert select(ref,[],[dict(item,context={'temperature':[.3,.4]})])[1]['kind']=='fallback'


def test_legacy_rejected_before_ports():
    with pytest.raises(ValueError, match='Legacy'):
        run({},dict(classes=[]))
    with pytest.raises(ValueError, match='No stable'):
        run({},dict(config(),history=[]))


class Experiment:
    def __init__(self, frequencies):
        self.policies={'current':SimpleNamespace(low=0.,high=1.)}
        self.commands=[]; self.events=[]; self.t=0.; self.frequencies=iter(frequencies)
    def clock(self):return self.t
    def emit(self,*args):self.events.append(args)
    def current_recovery(self,active):self.commands.append(('recover',active))
    def between_points(self):pass
    def move(self,axis,value,stage):self.commands.append((axis,value))
    def wait(self,seconds):self.t+=seconds
    def read(self,stage):
        self.t+=1.
        return dict(frequency_hz=next(self.frequencies),acquired_s=self.t,single_mode=False,
                    metrics={'bad_quality':100.},outputs=dict(current=.5,temperature=.2,piezo=30000.))


def test_frequency_success_does_not_require_single_mode_and_never_rearms_pi():
    p=config(); reference=infer(p['history'],12.,'a',p['reference'])
    e=Experiment([1001.,1002.,1000.])
    result=execute(e,p,reference)
    assert result['recovered'] and not result['pi_rearmed']
    assert not result['quality']['single_mode']
    assert [x for x in e.commands if x[0]=='current']==[('current',.4),('current',.5)]


def test_whole_route_validated_before_recovery_write():
    p=config(); p['fallback'][1]['offset']=1.
    e=Experiment([])
    with pytest.raises(DeviceError,match='envelope'):
        execute(e,p,infer(p['history'],12.,'a',p['reference']))
    assert e.commands==[]


def test_uncertain_write_never_replayed_or_rolled_back():
    p=config(); e=Experiment([])
    def fail(*args):raise RuntimeError('unconfirmed write')
    e.move=fail
    with pytest.raises(RuntimeError,match='unconfirmed'):
        execute(e,p,infer(p['history'],12.,'a',p['reference']))
    assert e.commands==[('recover',True)]


def test_stationary_accumulates_reference_but_target_change_invalidates_it():
    from ldcon.jobs.recovery_stationary import StationaryRecovery
    t=[0.]
    frequency=[1000.]
    lock=SimpleNamespace(response_info=lambda:dict(outputs=dict(current=.5,temperature=.2,piezo=30000.),
        observation=dict(frequency=dict(acquired_s=t[0],frequency_hz=frequency[0],unit='Hz'))))
    steady=dict(target_frequency_hz=1000.,recovery=config())
    r=StationaryRecovery(lock,steady,clock=lambda:t[0],sleep=lambda x:None,cancel=lambda:False,emit=lambda *x:None)
    r.prime()
    assert r.reference is None
    for i in range(12):
        t[0]=float(i)
        assert not r.probe()
    assert r.reference['frequency_hz']==1000.
    frequency[0]=1200.
    for i in range(12,15):
        t[0]=float(i)
        confirmed=r.probe()
    assert confirmed
    assert r.pending_reference['frequency_hz']==1000.
    steady['target_frequency_hz']=1200.
    t[0]=15.
    assert not r.probe()
    assert r.reference is None and r.pending_reference is None


def test_intentional_foreground_does_not_trigger_hop_recovery():
    from ldcon.jobs.recovery_stationary import StationaryRecovery
    t=[20.]
    lock=SimpleNamespace(response_info=lambda:dict(outputs=dict(current=.5,temperature=.2,piezo=30000.),
        observation=dict(frequency=dict(acquired_s=t[0],frequency_hz=5000.,unit='Hz'))))
    r=StationaryRecovery(lock,dict(target_frequency_hz=1000.,recovery=config()),clock=lambda:t[0],
        sleep=lambda x:None,cancel=lambda:False,emit=lambda *x:None)
    r.reference=infer(history(),12.,'a',settings())
    for i in range(20,25):
        t[0]=float(i)
        assert not r.probe(update_reference=False)
    assert r.hop_count==0


def test_real_typed_ports_prepare_and_recover_use_same_route_without_map():
    from .test_ecdl_recovery_card import Plant
    from ldcon.jobs.recovery_procedure import checked_recovery
    plant=Plant();plant.hop_center=.5;plant.now=12.
    p=config()
    outputs=dict(current=.5,temperature=.48,piezo=.46)
    p['history']=[dict(r,frequency_hz=plant.frequency(),outputs=outputs) for r in history()]
    p.update(execution='prepare',perturbations=[.03,-.03],tolerance_hz=1.)
    result=run(plant.ports,p,**plant.arguments)
    assert result['recovery_verified']
    artifact=checked_recovery(result['artifact'])
    assert artifact['schema']=='ldcon.ecdl-recovery.v2' and artifact['verified']
    assert len(artifact['trials'])==2
    assert all(t['hop_observed'] for t in artifact['trials'])
    assert {axis for axis,_ in plant.commands}=={'current'}
    # The same live algorithm admits a different starting branch without a map.
    plant.current.value=.53
    p['execution']='recover';p['perturbations']=[]
    result=run(plant.ports,p,**plant.arguments)
    assert result['recovered'] and plant.current.value==pytest.approx(.5)


def test_no_perturbations_publish_only_a_proposal():
    from .test_ecdl_recovery_card import Plant
    plant=Plant();plant.now=12.
    p=config();p['execution']='prepare'
    p['history']=[dict(r,frequency_hz=plant.frequency(),outputs=dict(current=.5,temperature=.48,piezo=.46)) for r in history()]
    result=run(plant.ports,p,**plant.arguments)
    assert not result['recovery_verified'] and plant.commands==[]


def test_task_model_proposal_is_data_not_authority():
    from ldcon.jobs.recovery_procedure import apply_proposal
    p=config()
    answer=dict(parameters=dict(fallback=[dict(offset=-.05,dwell_s=1.),dict(offset=0.,dwell_s=1.)]),reason='Measured smaller excursion succeeds')
    assert apply_proposal(p,answer)['fallback'][0]['offset']==-.05
    for change in (dict(fallback=[dict(offset=-.2,dwell_s=0.)]),dict(timeout_s=99999),dict(operation_epoch='other')):
        with pytest.raises(ValueError):apply_proposal(p,dict(parameters=change,reason='model claim'))


def test_draft_policy_loads_but_cannot_open_owner():
    from ldcon.jobs.recovery_procedure import require_configured
    assert parameters({'parameters':{}},{})['fallback'] is None
    with pytest.raises(ValueError,match='configured'):require_configured(parameters({'parameters':{}},{}))


def test_preparation_collects_its_own_fresh_baseline_without_prior_lock_history():
    from .test_ecdl_recovery_card import Plant
    plant=Plant()
    p=config();p.update(history=[],event_time_s=None,operation_epoch=None,execution='prepare')
    result=run(plant.ports,p,**plant.arguments)
    assert result['artifact']['reference']['samples']>=4
    assert result['artifact']['reference']['operation_epoch'].startswith('preparation:')
    assert not result['artifact']['verified'] and not plant.commands


def test_unix_acquisition_is_aligned_and_source_timestamp_retained():
    from ldcon.jobs.recovery_reference import observation
    row=observation(dict(acquisition_clock='unix',acquired_s=100000.,acquired_unix_s=100000.,
                         recorded_unix_s=100000.2,frequency_hz=1000.),40.)
    assert row['acquired_s']==pytest.approx(39.8)
    assert row['source_acquired_s']==100000. and row['source_acquisition_clock']=='unix'


def test_intentional_sweep_with_unchanged_target_clears_old_recovery_reference():
    from ldcon.jobs.recovery_stationary import StationaryRecovery
    t=[20.];frequency=[5000.]
    lock=SimpleNamespace(response_info=lambda:dict(outputs=dict(current=.5,temperature=.2,piezo=30000.),
        observation=dict(frequency=dict(acquired_s=t[0],frequency_hz=frequency[0],unit='Hz'))))
    r=StationaryRecovery(lock,dict(target_frequency_hz=1000.,recovery=config()),clock=lambda:t[0],
        sleep=lambda x:None,cancel=lambda:False,emit=lambda *x:None)
    r.reference=infer(history(),12.,'a',settings())
    r.probe(update_reference=False,intentional_operation=True)
    assert r.reference is None and r.pending_reference is None
    for step in range(21,27):
        t[0]=float(step)
        assert not r.probe(update_reference=True)
    assert r.reference['frequency_hz']==5000.


def test_standalone_and_stationary_reject_qualification_mode_before_ports():
    from ldcon.jobs.ecdl_mode_recovery import run_mode_recovery
    from ldcon.jobs.ecdl_steady import parameters as steady_parameters
    p=config();p.update(execution='prepare',perturbations=[.01])
    with pytest.raises(ValueError,match='cannot execute preparation'):
        run_mode_recovery({},p)
    with pytest.raises(ValueError,match='cannot execute preparation'):
        steady_parameters({'parameters':{}},{'recovery':p})


def test_prepared_policy_strips_trials_and_reference_for_new_execution():
    from .test_ecdl_recovery_card import Plant
    from ldcon.jobs.recovery_procedure import policy_from_artifact
    plant=Plant();plant.now=12.
    p=config();p['execution']='prepare'
    result=run(plant.ports,p,**plant.arguments)
    policy=policy_from_artifact(result['artifact'])
    assert policy['execution']=='recover' and policy['execution_mode']=='deterministic'
    assert policy['perturbations']==policy['history']==[] and policy['operation_epoch'] is None
    assert policy['fallback']==p['fallback']
