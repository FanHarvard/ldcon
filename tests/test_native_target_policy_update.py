"""Protected decisions may add only the optional target-response policy fields."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ldcon.owners.esp32_native_owner import C_NM, digest
from ldcon.core.instrument_client import BrokerError
from ldcon.owners.native_policy_update import journal_path

_spec=importlib.util.spec_from_file_location('target_policy_fixture',
    (Path(__file__).resolve().parent / 'support')/'jobs/tests/test_esp32_native_current.py')
_fixture=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


@pytest.fixture
def rig(tmp_path,monkeypatch,request):
    options=getattr(request,'param',{})
    profile=_fixture.policy()
    profile['optical']['expected_laser']=options.get('optical_label','test')
    monkeypatch.setattr(_fixture,'policy',lambda:copy.deepcopy(profile))
    monkeypatch.delenv('LDCON_BACKGROUND_LASER_ID',raising=False)
    result=_fixture.rig.__wrapped__(tmp_path)
    if not options.get('no_snapshot'):
        # Model the host-validated, immutable startup factory binding.
        snapshot=dict(id=options.get('canonical_id','test'),
            interfaces=dict(native_current=dict(device='native')),
            devices=dict(native=dict(type='esp32servocon.native_current.owner',
                connection=dict(device_id=result[0].driver.device_id))))
        result[0].system_owner.assemblies.factory=SimpleNamespace(laser_snapshot=snapshot)
    return result


def decision(rig,tmp_path,*,authority='human_approval',changes=None,laser_id='test'):
    owner=rig[0]
    owner.policy_config_file=tmp_path/'owner-config.json'
    owner.policy_config_file.write_text(json.dumps({'native_current':owner.policy}))
    owner.approvals_root=tmp_path/'approvals'
    path=owner.approvals_root/laser_id/'decisions'/('a'*32+'.json')
    path.parent.mkdir(parents=True)
    fields=changes if changes is not None else {
        'native.target_feedforward_scale':(None,1.),
        'native.target_feedforward_scale_bounds':(None,[.5,2.]),
        'native.target_ramp_step_hz':(None,100e6)}
    value=dict(schema_version=1,id='a'*32,laser_id=laser_id,authority=authority,
        scope=dict(kind='lasers',name=laser_id),human_name='offline human approval',
        changes=[dict(path='devices.native.control_settings.native_current.'+key,old=old,new=new)
                 for key,(old,new) in fields.items()])
    path.write_text(json.dumps(value))
    owner._persist()
    return dict(action='policy_update',device_id=owner.driver.device_id,
        expected_definition=owner.definition,decision_id=value['id'],laser_id=laser_id)


def test_exact_protected_decision_adds_optional_fields_without_hardware_writes(rig,tmp_path):
    owner,servo,*_=rig
    original=copy.deepcopy(owner.policy)
    request=decision(rig,tmp_path)
    writes=copy.deepcopy(servo.writes)
    prepared=owner.dispatch({**request,'action':'policy_update_prepare'})
    assert prepared['status']=='prepared' and owner.policy==original
    assert not journal_path(owner.state_file).exists()
    result=owner.dispatch(request)
    assert result['status']=='updated' and servo.writes==writes
    assert owner.policy['native']['target_feedforward_scale_bounds']==[.5,2.]
    assert owner.policy['calibration']==original['calibration']
    assert owner.policy['native']['capture_hz']==original['native']['capture_hz']
    # Rule M step 4: config.json is layer 1 only; the live update never writes a policy copy into it.
    assert json.loads(owner.policy_config_file.read_text())['native_current']==original
    assert json.loads(owner.state_file.read_text())['definition']==digest(owner.policy)
    assert json.loads(journal_path(owner.state_file).read_text())['status']=='committed'
    assert owner.dispatch(request)['idempotent'] is True and servo.writes==writes


@pytest.mark.parametrize('case',['unapproved','missing_other','wrong_old','wrong_laser','invalid_bounds'])
def test_additions_cannot_bypass_approval_old_value_scope_or_validation(rig,tmp_path,case):
    owner,servo,*_=rig
    options={}
    if case=='unapproved':options['authority']='free'
    elif case=='missing_other':options['changes']={'native.acceptance_hz':(None,30e6)}
    elif case=='wrong_old':options['changes']={'native.target_feedforward_scale':(1.,1.1)}
    elif case=='wrong_laser':options['laser_id']='another-laser'
    else:options['changes']={'native.target_feedforward_scale_bounds':(None,[2.,.5])}
    request=decision(rig,tmp_path,**options)
    original=copy.deepcopy(owner.policy);writes=copy.deepcopy(servo.writes)
    with pytest.raises((BrokerError,ValueError)):
        owner.dispatch(request)
    assert owner.policy==original and servo.writes==writes and not journal_path(owner.state_file).exists()


@pytest.mark.parametrize('busy',['trial','ramp','feedforward'])
def test_target_policy_changes_wait_for_active_experiment(rig,tmp_path,busy):
    owner,servo,clock,bias,optical,gate,call,ready=rig
    ready();call('arm')
    if busy=='trial':
        status=call('target_ff_study_status')
        call('target_ff_study_set',expected_revision=status['revision'],scale=1.,step_hz=100e6,duration_s=60.)
    elif busy=='ramp':
        call('retarget',target_frequency_hz=C_NM/owner.prepared['target_nm']+20e6)
    else:
        owner.ff={'state':'active'}  # Admission guard only: rejected before any FF proof/read.
    request=decision(rig,tmp_path)
    original=copy.deepcopy(owner.policy);writes=copy.deepcopy(servo.writes)
    with pytest.raises(BrokerError,match='Finish the active target/FF'):
        owner.dispatch(request)
    assert owner.policy==original and servo.writes==writes


@pytest.mark.parametrize('rig',[dict(optical_label='Sr1092',canonical_id='sr1092')],indirect=True)
def test_canonical_document_id_is_independent_of_optical_display_label(rig,tmp_path):
    owner,servo,*_=rig
    request=decision(rig,tmp_path,laser_id='sr1092')
    writes=copy.deepcopy(servo.writes)
    result=owner.dispatch(request)
    assert result['status']=='updated' and servo.writes==writes
    assert owner.policy['optical']['expected_laser']=='Sr1092'


@pytest.mark.parametrize('rig',[dict(optical_label='Sr1092',no_snapshot=True)],indirect=True)
def test_trusted_host_identity_supports_owner_without_assembly(rig,tmp_path,monkeypatch):
    monkeypatch.setenv('LDCON_BACKGROUND_LASER_ID','sr1092')
    request=decision(rig,tmp_path,laser_id='sr1092')
    assert rig[0].dispatch(request)['status']=='updated'


@pytest.mark.parametrize('damage',['conflicting_host','wrong_device','missing_binding','display_label'])
def test_identity_binding_cannot_be_guessed_or_redirected(rig,tmp_path,monkeypatch,damage):
    owner,servo,*_=rig
    laser_id='test'
    if damage=='conflicting_host':monkeypatch.setenv('LDCON_BACKGROUND_LASER_ID','another-laser')
    elif damage=='wrong_device':
        owner.system_owner.assemblies.factory.laser_snapshot['devices']['native']['connection']['device_id']='another-device'
    elif damage=='missing_binding':owner.system_owner.assemblies.factory=None
    else:laser_id='Test'  # No case-insensitive alias even when letters match.
    request=decision(rig,tmp_path,laser_id=laser_id)
    original=copy.deepcopy(owner.policy);writes=copy.deepcopy(servo.writes)
    with pytest.raises(BrokerError,match='binding|identity|another laser'):
        owner.dispatch(request)
    assert owner.policy==original and servo.writes==writes
