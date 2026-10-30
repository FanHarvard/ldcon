"""Proposal sizes cannot confer authority or become additional execution gates."""
import copy
import json
from types import SimpleNamespace

import pytest

from experiment_core.adjustment_guidance import instructions
from ldcon.agents.adjustment_guidance import read, current, validate, location, update
from experiment_core.records import atomic_json
from threading import RLock
from ldcon.agents.agent_bundle import build


def guidance(step=.0002):
    return dict(schema_version=1,knobs=dict(current=dict(kind='absolute',step=step,unit='V',
        reason='Provisional local probe; revise using comparable measured sensitivity')))


def test_units_and_explicit_advisory_role(tmp_path):
    path=location(tmp_path,'sr422');path.parent.mkdir(parents=True);atomic_json(path,guidance())
    value=read(tmp_path,'sr422')
    assert value['advisory_only'] is True and value['document']['knobs']['current']['step']==.0002
    assert 'not a minimum, maximum' in value['interpretation']
    assert read(tmp_path,'sr1092')['availability']=='not_configured'
    assert 'allowed range' in instructions() and 'Characterization' in instructions()


@pytest.mark.parametrize('change',[
    dict(step=float('nan')),dict(step=float('inf')),dict(step=True),dict(step=-1),
    dict(unit='mV'),dict(kind='maximum'),dict(max_step=1),dict(reason=''),
    dict(kind='relative',unit='V'),
])
def test_invalid_advice_cannot_hide_a_bound_or_ambiguous_unit(change):
    data=guidance();data['knobs']['current'].update(change)
    with pytest.raises(ValueError):validate(data)


def test_advice_does_not_require_step_inside_trial_bounds():
    # Validation checks structure and units, NOT physical admissibility. Owners
    # remain responsible for admitting actual candidates within their limits.
    assert validate(guidance(100))['knobs']['current']['step']==100
    value=guidance();value['knobs']['unknown_actuator']=value['knobs'].pop('current')
    with pytest.raises(ValueError):validate(value)


def test_relative_step_fraction_and_hot_read_without_safety_change(tmp_path):
    laser=dict(id='sr422',devices={'rig':{'absolute_conditions':{'range':[0,1]}}})
    policy=copy.deepcopy(laser)
    runtime=SimpleNamespace(root=tmp_path,mutex=RLock(),interface_repository=SimpleNamespace(read=lambda *args:dict(document=laser)))
    old=current(runtime,'sr422')
    document=guidance()
    document['knobs']['tec_gain']=dict(kind='relative',step=.1,unit='fraction',reason='Initial comparison')
    new=update(runtime,'sr422',dict(document=document,expected_revision=old['revision']))
    assert new['revision']!=old['revision'] and laser==policy
    assert current(runtime,'sr422')==new
    from ldcon.web.web_runtime import ConflictError
    with pytest.raises(ConflictError):update(runtime,'sr422',dict(document=document,expected_revision=old['revision']))
    assert list(tmp_path.rglob('*.json'))==[location(tmp_path,'sr422')]


def test_every_task_bundle_records_same_advice_contract(tmp_path):
    path=location(tmp_path,'sr422');path.parent.mkdir(parents=True);atomic_json(path,guidance())
    expected=read(tmp_path,'sr422')
    (tmp_path/'adjustment-guidance-at-start.json').write_text(json.dumps(expected))
    for profile in ('diagnosis','adjustment','research'):
        b=build(tmp_path,profile,context=dict(laser_id='sr422'),evidence={})
        assert b['input']['context']['adjustment_guidance_at_card_start']==expected
        assert instructions() in b['instructions']
