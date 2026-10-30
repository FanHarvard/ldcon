"""Resolved recovery policy is required before any live owner can be opened."""
import copy
from unittest.mock import Mock

import pytest

from ldcon.jobs import recovery_procedure as procedure
from ldcon.jobs import recovery_reference as reference
from ldcon.jobs.ecdl_mode_recovery import parameters as recovery_parameters
from ldcon.jobs.ecdl_recovery import parameters as preparation_parameters
from .test_recovery_reference_v2 import config, settings


@pytest.mark.parametrize('field', sorted(procedure.REQUIRED_EXECUTION))
def test_missing_execution_value_rejected_before_owner_even_after_normalization(field, monkeypatch):
    policy=config(); policy.pop(field)
    draft=procedure.parameters({'parameters':policy},{})
    assert draft[field] is None
    owner=Mock()
    monkeypatch.setattr(procedure,'BoundedExperiment',owner)
    with pytest.raises(ValueError,match='configured explicitly'):
        procedure.run({},draft)
    owner.assert_not_called()


@pytest.mark.parametrize('field',sorted(reference.DEFAULTS))
def test_reference_fields_have_no_partial_runtime_defaults(field):
    policy=settings();policy.pop(field)
    with pytest.raises(ValueError,match='Explicit recovery reference settings'):
        reference.parameters(policy)
    p=config();p['reference']=policy
    with pytest.raises(ValueError,match='Explicit recovery reference settings'):
        procedure.run({},p)


def test_agent_deadline_is_required_only_for_agent_execution(monkeypatch):
    p=config();p.update(execution='prepare',execution_mode='agent')
    owner=Mock();monkeypatch.setattr(procedure,'BoundedExperiment',owner)
    with pytest.raises(ValueError,match='agent deadline.*explicitly'):
        procedure.run({},p,decide=Mock())
    owner.assert_not_called()
    p['agent_timeout_s']=300.
    assert procedure.require_configured(p)['agent_timeout_s']==300.
    p.update(execution_mode='deterministic',agent_timeout_s=None)
    assert procedure.require_configured(p)['agent_timeout_s'] is None


def test_draft_public_cards_load_but_do_not_invent_operational_policy():
    recover=recovery_parameters({'parameters':{}},{})
    prepare=preparation_parameters({'parameters':{}},{})
    assert recover['execution']=='recover' and recover['execution_mode']=='deterministic'
    assert prepare['execution']=='prepare' and prepare['execution_mode'] is None
    for draft in (recover,prepare):
        for key in ('stable_s','confirm_samples','sample_interval_s','max_sample_age_s',
                    'measurement_timeout_s','timeout_s','hop_threshold_hz','tolerance_hz'):
            assert draft[key] is None
        with pytest.raises(ValueError,match='configured'):
            procedure.require_configured(draft)


def test_resolved_settings_preserved_without_mutation():
    supplied=config();saved=copy.deepcopy(supplied)
    resolved=procedure.require_configured(supplied)
    assert resolved==supplied==saved
    resolved['reference']['guard_s']=9.
    assert supplied==saved
