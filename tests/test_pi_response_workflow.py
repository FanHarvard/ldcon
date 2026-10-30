import copy
import json
from types import SimpleNamespace

import pytest
import yaml

from ldcon.jobs import pi_response as workflow, settling_profile, learned_settling
from ldcon.jobs.background_pid_session import _CandidateTuner
from ldcon.jobs.online_pid import parameters


def test_candidate_comparator_has_no_lock_tolerance_objective(tmp_path):
    tuner=_CandidateTuner(lambda:None,lambda _:None,lambda *_:None,lambda:0.)
    tuner.p=parameters({'parameters':{}},{})
    tuner.phase='baseline';tuner.trial_index=0
    port=SimpleNamespace(config={'resolution':1.},pid={},session=SimpleNamespace(directory=tmp_path,segment=0))
    def rows(error,band):
        return [(float(i),dict(error=error+(-1)**i*100.,output=i%2,limited=False,
            filtered_error=error,control_deadband=band,requested_tolerance=band,
            integral_saturated=False,integral_usage=.5)) for i in range(60)]
    for band in (1.,10e6,1e9):
        base=tuner._metrics(rows(5e6,band),port)
        better=tuner._metrics(rows(2e6,band),port)
        assert tuner._healthy(rows(5e6,band)[0][1])
        assert tuner._accept(better,base,rows(2e6,band)[0][1])
        assert base['score']==pytest.approx(25e12+10000)
        assert not base['lock_tolerance_used']
    assert not tuner._healthy(dict(limited=True,error=0.))


def test_threshold_is_not_a_pi_response_input():
    with pytest.raises(ValueError,match='no convergence threshold'):
        workflow.parameters({'parameters':{}},{'tolerance_hz':10e6})


@pytest.fixture
def rig(tmp_path,monkeypatch):
    directory=tmp_path/'jobs/test'/('a'*32)
    directory.mkdir(parents=True)
    cards=[dict(id=i,operation=o,parameters={}) for i,o in (
        ('diagnostic-agent','agent.resident'),('agent-adjust','agent.task'),('target-response','measure.target_response'))]
    (directory/'interface-deck.yaml').write_text(yaml.safe_dump(dict(id='deck',cards=cards)))
    (directory/'job.json').write_text(json.dumps(dict(set_id='test')))
    laser=dict(id='test',allow_output=True,target=dict(frequency_hz=300e12,max_change_hz=100e6,bounds_hz=[299e12,301e12]),
               devices={'native':{'type':'esp32servocon.native_current.owner'}})
    clock=[0.];writes=[];children=[];proof=[True];valid=[True];revision=['r'];stopped=[]
    class API:
        def get_configuration(self,*_):return dict(document=copy.deepcopy(laser),revision=revision[0])
        def _request(self,method,path,body=None):
            if path=='api/sets/test':return {'revision':'set-r'}
            if path.endswith('/start'):
                child=('b' if not children else 'c')*32;folder=directory.parent/child;folder.mkdir()
                if not children:
                    owner={'latest_result':{'state':'unchanged','comparison':{'accepted':False},'owner_receipt':{'last':{'state':'restored'}}}} if proof[0] else {}
                    (folder/'resident-adjustment-result.json').write_text(json.dumps(dict(owner_result=owner)))
                else:
                    (folder/'target-response.json').write_text(json.dumps(dict(restoration='starting_target_confirmed',
                        final_configuration_revision='r',trials=[{'index':i} for i in range(4)])))
                children.append((child,body));return {'id':child}
            if path.endswith('/stop'):stopped.append(path);return {}
            if method=='GET' and path.startswith('api/jobs/'):
                return dict(status='succeeded')
            raise AssertionError((method,path))
    from ldcon.jobs import wavelength_history
    monkeypatch.setattr(wavelength_history,'configured_reader',lambda _:None)
    monkeypatch.setattr(workflow,'refresh_recorded_history',lambda measured,reader:(measured,[]))
    monkeypatch.setattr(settling_profile,'context',lambda _: {'actual_pi':[1,2,3]})
    monkeypatch.setattr(settling_profile,'install',lambda *args: writes.append(args) or {'revision':'learned'})
    monkeypatch.setattr(learned_settling,'fit',lambda *args: {'status':'validated' if valid[0] else 'inconclusive','context_revision':'x'})
    monkeypatch.setattr(learned_settling,'matches_context',lambda *args:True)
    def run():return workflow.run_card(directory,laser,{'id':'pi-response'},workflow.parameters({'parameters':{}},{}),
        clock=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+s),cancel=lambda:False,emit=lambda *_:None,api=API())
    return run,writes,children,proof,valid


def test_composition_compares_pi_before_independent_response_and_profile_install(rig):
    run,writes,children,proof,valid=rig
    result=run()
    assert result['status']=='succeeded' and len(writes)==1
    assert [b['card_id'] for _,b in children]==['agent-adjust','target-response']
    assert children[1][1]['parameters']['offsets_hz']==[100e6,0.,100e6,0.]
    assert children[1][1]['parameters']['execution_mode']=='deterministic'


def test_no_trial_decision_never_becomes_pi_optimization_success(rig):
    run,writes,children,proof,valid=rig;proof[0]=False
    assert run()['status']=='inconclusive'
    assert len(children)==1 and not writes


def test_rejected_heldout_profile_does_not_replace_active_completion(rig):
    run,writes,children,proof,valid=rig;valid[0]=False
    assert run()['status']=='inconclusive' and not writes


def test_justified_no_trial_can_characterize_but_never_claim_pi_comparison(rig,monkeypatch):
    run,writes,children,proof,valid=rig;proof[0]=False
    read=workflow.read_json
    def evidence(path,*args):
        if path.name=='resident-adjustment-result.json':return {}
        if path.name=='agent-task-status.json':
            return dict(status='succeeded',disposition='no_adjustment',physical_outcome='no_action_submitted',reason='Measured noise limits candidate resolution')
        return read(path,*args)
    monkeypatch.setattr(workflow,'read_json',evidence)
    result=run()
    assert result['status']=='succeeded' and not result['pi_comparison_confirmed']
    assert result['pi_outcome']=='retained_without_candidate_comparison'
    assert len(children)==2 and len(writes)==1


def test_historical_refresh_only_adds_identical_source_rows():
    old=dict(provenance={'source':'one'},samples=[{'time_unix_s':1.,'wavelength_nm':1000.,'system_seq':1}])
    m={'parameters':{'baseline_s':1.},'trials':[{'index':0,'requested_unix_s':1.,'record_end_unix_s':2.,'history':old}]}
    new=copy.deepcopy(old);new['samples'].append({'time_unix_s':2.,'wavelength_nm':1001.,'system_seq':2})
    calls=[]
    def read(a,b):calls.append((a,b));return new
    updated,proof=workflow.refresh_recorded_history(m,SimpleNamespace(read=read))
    assert calls==[(0.,2.)] and len(updated['trials'][0]['history']['samples'])==2
    assert len(m['trials'][0]['history']['samples'])==1 and proof[0]['exact_overlap']
    new['samples'][0]['wavelength_nm']=1002.
    with pytest.raises(Exception,match='changed'):workflow.refresh_recorded_history(m,SimpleNamespace(read=read))


def test_historical_refresh_rejects_source_and_outside_interval():
    old=dict(provenance={'source':'one'},samples=[{'time_unix_s':1.,'wavelength_nm':1000.}])
    m={'parameters':{'baseline_s':1.},'trials':[{'index':0,'requested_unix_s':1.,'record_end_unix_s':2.,'history':old}]}
    new=copy.deepcopy(old);new['provenance']['source']='two'
    with pytest.raises(Exception,match='changed'):workflow.refresh_recorded_history(m,SimpleNamespace(read=lambda *_:new))
    new=copy.deepcopy(old);new['samples'].append({'time_unix_s':3.,'wavelength_nm':1000.})
    with pytest.raises(Exception,match='outside'):workflow.refresh_recorded_history(m,SimpleNamespace(read=lambda *_:new))
