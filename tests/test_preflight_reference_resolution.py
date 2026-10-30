"""Preflight resolves alignment evidence without reviving retired steady references."""
from types import SimpleNamespace
import pytest
from ldcon.web import laser_operations
from ldcon.jobs import workflow_artifacts


@pytest.mark.parametrize('operation,resolve',[
    ('tune.ecdl_operating_point',True),('servo.ecdl_steady_lock',False)])
def test_preflight_resolves_only_current_alignment_inputs(monkeypatch,operation,resolve):
    card={'id':'card','operation':operation}
    value={'id':'laser','laser_definition':{'document':{'id':'laser'},'revision':'laser-revision'}}
    definition={'id':'deck','document':{'cards':[card]},'invocation_parameters':{'card':{'original':True}}}
    runtime=SimpleNamespace(active_jobs=lambda **kw:[])
    port=laser_operations.LaserOperations(SimpleNamespace(runtime=runtime),'laser')
    monkeypatch.setattr(port,'_selection',lambda p:(value,definition,card,{}))
    seen=[]
    def resolver(runtime,laser,p):
        seen.append(laser)
        return dict(p,resolved=True)
    monkeypatch.setattr(workflow_artifacts,'resolve_inputs',resolver)
    monkeypatch.setattr(laser_operations,'validate_card',lambda laser,deck,card,p:dict(parameters=p,blocked_reasons=[],runnable=True))
    monkeypatch.setattr(laser_operations,'admission_class',lambda *a:'exclusive')
    proposal=dict(set_id='laser',deck_id='deck',card_id='card',expected_revision='revision')
    result=port.preflight(proposal,laser_operations.TrustedOperationContext('laser'))
    assert seen==(['laser'] if resolve else [])
    assert result['parameters'].get('resolved',False)==resolve
    assert result['runnable'] and result['physical_validation']=='owner_revalidation_required'
