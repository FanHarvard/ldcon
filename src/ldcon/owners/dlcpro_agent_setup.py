"""Owner-side commissioning records for attached adjustment paths.

These optional records come from the trusted service configuration, never a task
proposal. Validation is metadata-only and does not open a device connection.
"""
from copy import deepcopy
from types import SimpleNamespace

from ldcon.owners.dlcpro_parameter_trial import qualification


def evidence(config, laser_id, channel, policy):
    records = config.get('ff_adjustments', {})
    if not isinstance(records, dict):
        raise ValueError('FF adjustment records must be keyed by laser ID')
    record = records.get(laser_id)
    if record is None:return None
    owner = SimpleNamespace(channel=channel, policy=policy,
        instrument=SimpleNamespace(hardware=config['hardware']), ff_adjustment_evidence=deepcopy(record))
    checked = qualification(owner)
    identity = checked['evidence']['identity']
    if (identity['serial_number'] != config['expected_serial_number']
            or identity['firmware_version'] != config.get('expected_firmware_version')):
        raise ValueError('FF adjustment identity differs from the commissioned chassis/firmware')
    if config['hardware']['channels'][str(channel)]['laser_id'] != laser_id:
        raise ValueError('FF adjustment channel belongs to another laser')
    return checked


def validate_config(config, lasers):
    records = config.get('ff_adjustments', {})
    if not isinstance(records, dict) or set(records)-{laser['id'] for laser in lasers}:
        raise ValueError('FF adjustment record names an unknown laser')
    from ldcon.owners.dlcpro_owner import policy_from_device
    for laser in lasers:
        device = laser['devices']['dlc']
        evidence(config, laser['id'], device['connection']['channel'], policy_from_device(device))


def attach(protocol, config):
    owner = protocol.owner
    record = evidence(config, owner.laser_id, owner.channel, owner.policy)
    if record is not None:owner.ff_adjustment_evidence = record
