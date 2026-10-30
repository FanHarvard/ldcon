"""Optional FF commissioning records are pinned to one declared chassis."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ldcon.owners.dlcpro_agent_setup import attach, validate_config
from .test_dlcpro_offline import rig


def configuration(rig):
    owner = rig['owner']
    hardware = deepcopy(owner.instrument.hardware)
    hardware['channels']['1']['ff_delta_verified'] = True
    proof = dict(tolerance_ma=.01, evidence=dict(identity=dict(serial_number='DLC-test',firmware_version='1.9'),
        pzt_hardware_bounds_v=[0.,140.],
        pzt_delta_qualification=dict(verified=True,channel=1,source='approved-offline-qualification')))
    return dict(hardware=hardware,expected_serial_number='DLC-test',expected_firmware_version='1.9',
        ff_adjustments={owner.laser_id:proof})


def test_explicit_valid_record_attaches_without_io_or_shared_mutable_reference(rig):
    config = configuration(rig); owner = rig['owner']
    owner.instrument.hardware = deepcopy(config['hardware'])
    before = len(rig['driver'].writes)
    validate_config(config, [rig['laser']])
    attach(SimpleNamespace(owner=owner),config)
    assert owner.ff_adjustment_evidence == config['ff_adjustments'][owner.laser_id]
    config['ff_adjustments'][owner.laser_id]['tolerance_ma'] = .1
    assert owner.ff_adjustment_evidence['tolerance_ma'] == .01
    assert len(rig['driver'].writes)==before


@pytest.mark.parametrize('case', ['serial', 'firmware', 'laser', 'channel', 'proof'])
def test_foreign_or_unqualified_records_refused_before_service_connection(rig, case):
    config = configuration(rig); proof = config['ff_adjustments'][rig['owner'].laser_id]
    if case=='serial':proof['evidence']['identity']['serial_number']='other'
    if case=='firmware':config['expected_firmware_version']='3.2'
    if case=='laser':config['ff_adjustments']['another-laser']=deepcopy(proof)
    if case=='channel':config['hardware']['channels']['1']['laser_id']='other'
    if case=='proof':proof['evidence']['pzt_delta_qualification']['verified']=False
    with pytest.raises(ValueError):validate_config(config,[rig['laser']])


def test_optional_record_absence_does_not_invent_ff_authority(rig):
    config = configuration(rig); del config['ff_adjustments']
    validate_config(config,[rig['laser']])
    attach(SimpleNamespace(owner=rig['owner']),config)
    assert not hasattr(rig['owner'],'ff_adjustment_evidence')
