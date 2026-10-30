"""Active v2 recovery never looks up branch maps and rejects old inputs."""
from unittest.mock import Mock, patch
import pytest
from ldcon.jobs import ecdl_mode_recovery, ecdl_steady
from .test_recovery_reference_v2 import config, Experiment
from ldcon.jobs.recovery_procedure import execute
from ldcon.jobs.recovery_reference import infer


@pytest.mark.parametrize('branch_match',[None,{}, {'max_age_s':86400}])
def test_old_map_request_rejected_before_port_access(branch_match):
    ports=Mock()
    with pytest.raises(ValueError,match='Legacy'):
        ecdl_mode_recovery.run_mode_recovery(ports,dict(branch_match=branch_match))
    assert not ports.mock_calls
    with pytest.raises(ValueError,match='Legacy'):
        ecdl_steady.parameters({'parameters':{}},{'recovery':dict(procedure=dict(branch_match=branch_match))})


def test_recovery_executes_without_map_loading_or_matching():
    p=config(); e=Experiment([1001.,1001.,1001.])
    with patch('ldcon.jobs.branch_maps.load_for_job',side_effect=AssertionError('map read')), \
         patch('ldcon.jobs.ecdl_branch_recovery.match_branch',side_effect=AssertionError('map match')):
        result=execute(e,p,infer(p['history'],12.,'a',p['reference']))
    assert result['recovered']
