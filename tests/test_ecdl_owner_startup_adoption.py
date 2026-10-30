"""sr422 rig owner start-up (hardware findings 2026-10-27 20:25-20:36, preparations/sr422-relock-20261027/LOG.md):

(2) a refused restart adoption leaves the ORIGINAL durable record untouched (the refusal goes to a sibling file) and a
    TRANSIENT refusal (TypeError/OSError/BrokerError) is re-attempted from that record at the next start;
(3) an idle start adopts a laser yaml that differs from the saved authority policy (rule M step 4 for the rig owner),
    while a persisted lock context keeps the start refusal with a precise message.
Real ECDLOwner / NativeLockOwner over the SCPI + esp32servocon emulator of owner/tests/fake_rig.py; no hardware,
no network. Fixtures shared with test_ecdl_owner_readopt.py."""
import copy
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from experiment_core.errors import StaleContext
from ldcon.core.instrument_client import BrokerError
from ldcon.core.interface_schema import digest
from ldcon.owners.ecdl_owner import ECDLOwner
from ldcon.owners.ecdl_steady_owner import NativeLockOwner

_spec = importlib.util.spec_from_file_location('ecdl_readopt_tests', Path(__file__).with_name('test_ecdl_owner_readopt.py'))
readopt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(readopt)
fake_rig, Rig, Clock = readopt.fake_rig, readopt.Rig, readopt.Clock
make_owner, lock, shutdown, restart, call, audit_kinds = (readopt.make_owner, readopt.lock, readopt.shutdown,
                                                          readopt.restart, readopt.call, readopt.audit_kinds)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('No hardware/network access in offline test')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    import ldcon.drivers.siglent as siglent
    monkeypatch.setattr(siglent, 'VisaTransport', fake_rig.Visa)
    monkeypatch.setattr(NativeLockOwner, 'watch_enabled', False)
    monkeypatch.setattr(NativeLockOwner, 'passive_dither', lambda self: None)


@pytest.fixture
def rig(tmp_path):
    clock, holder = Clock(), {}
    owner = make_owner(tmp_path, clock, holder)
    lease = owner.dispatch(dict(action='claim', revision=digest(owner.device), owner='test'))['lease']
    return owner, holder, clock, lease


def restart_with(tmp_path, clock, holder, factory_error=None, laser=None):
    """A new owner process over the same emulated instruments; ``factory_error`` is raised by the hardware factory
    (the 2026-10-27 20:27 defect: the default factory lambda refused the adopt_leases keyword)."""
    previous = holder.get('hw')
    def factory(device, adopt_leases=None):
        if factory_error is not None:
            raise factory_error
        holder['hw'] = Rig(device, clock, previous=previous, adopt_leases=adopt_leases)
        return holder['hw']
    return ECDLOwner(laser or fake_rig.laser_definition(), audit_path=tmp_path / 'audit.jsonl', factory=factory, clock=clock)


def audit_rows(owner, kind):
    return [json.loads(line) for line in owner.audit_path.read_text().splitlines() if json.loads(line)['kind'] == kind]


# ---- (2) refusal keeps the original record; transient refusals are retried ---------------------------------------

def test_transient_refusal_keeps_the_original_record_and_the_next_start_adopts_from_it(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    shutdown(owner)
    original = json.loads(owner.state_path.read_text())
    assert original['active'] and original['steady']['profile'] and original['steady']['expected_servo']
    writes = len(hw.firmware.sets)
    refused = restart_with(tmp_path, clock, holder, factory_error=TypeError("ECDLOwner.__init__.<locals>.<lambda>() got an unexpected keyword argument 'adopt_leases'"))
    assert refused.fault and 'adoption refused' in refused.fault and 'adopt_leases' in refused.fault
    assert refused.adoption['state'] == 'refused' and refused.adoption['transient'] is True and refused.lease == lease
    # the durable record is untouched (the steady context survives for the retry / the review)
    assert json.loads(owner.state_path.read_text()) == original
    refusal = json.loads(refused.refused_path.read_text())
    assert refusal['transient'] is True and refusal['original'] == original and refusal['lease'] == lease
    assert 'adopt_leases' in refusal['reason'] and refusal['adoption']['state'] == 'refused'
    quarantine = json.loads(refused.quarantine_path.read_text())
    assert quarantine['adoption']['transient'] is True and quarantine['lease'] == lease
    # nothing later in the refused process overwrites the record either
    refused.persist_state()
    assert json.loads(owner.state_path.read_text()) == original
    with pytest.raises(BrokerError, match='adoption refused'):
        call(refused, 'steady_heartbeat')
    assert len(hw.firmware.sets) == writes and hw.firmware.cfg['pid_multiplier'] == 1
    # the fixed owner starts: the adoption is re-attempted from the ORIGINAL record and succeeds (read-only)
    clock.advance(5.)
    fixed = restart_with(tmp_path, clock, holder)
    assert fixed.fault is None and fixed.lease == lease and fixed.steady.armed and fixed.steady.active
    assert fixed.adoption['state'] == 'adopted' and not fixed.quarantine_path.exists()
    assert fixed.steady.target == original['steady']['target'] and fixed.steady.expected_servo == original['steady']['expected_servo']
    kinds = audit_kinds(fixed)
    assert kinds.index('owner_restart_adoption_retried') < kinds.index('owner_restart_adopted')
    assert list(tmp_path.glob('adoption-retried-quarantine-*.json'))
    assert len(hw.firmware.sets) == writes and hw.firmware.cfg['pid_multiplier'] == 1
    assert call(fixed, 'steady_heartbeat') is True and call(fixed, 'steady_read')['state'] == 'locked'
    # a refusal file of the retried attempt is not left behind once the adoption succeeded and the lease ends
    call(fixed, 'steady_hold', reason='test')
    assert call(fixed, 'release') is True
    assert not fixed.state_path.exists() and not fixed.refused_path.exists()


def test_unreachable_rig_refusal_is_transient_and_retried_when_the_rig_answers(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    shutdown(owner)
    original = json.loads(owner.state_path.read_text())
    get = hw.firmware.get
    def unreachable(path):
        raise OSError(111, 'Connection refused')
    hw.firmware.get = unreachable
    down = restart(owner, tmp_path, clock, holder)
    clock.advance(91.)
    assert down.steady.watch_tick() is False and down.adoption['state'] == 'refused' and down.adoption['transient'] is True
    assert json.loads(owner.state_path.read_text()) == original
    hw.firmware.get = get
    up = restart_with(tmp_path, clock, holder)
    assert up.adoption['state'] == 'adopted' and up.fault is None and up.lease == lease
    assert 'owner_restart_adoption_retried' in audit_kinds(up)


def test_definitive_refusal_keeps_the_record_for_the_review_and_is_not_retried(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    shutdown(owner)
    original = json.loads(owner.state_path.read_text())
    hw.firmware.cfg['pid_multiplier'] = 0                   # a human stopped the PI at the device: the rig differs
    writes = len(hw.firmware.sets)
    refused = restart(owner, tmp_path, clock, holder)
    assert refused.adoption['state'] == 'refused' and refused.adoption['transient'] is False
    assert 'Native PI ownership lost' in refused.fault
    assert json.loads(owner.state_path.read_text()) == original          # kept for the offline review
    assert json.loads(refused.refused_path.read_text())['transient'] is False
    again = restart(refused, tmp_path, clock, holder)
    assert again.fault == refused.fault and again.adoption['state'] == 'refused' and again.lease == lease
    assert 'owner_restart_adoption_retried' not in audit_kinds(again)
    assert json.loads(owner.state_path.read_text()) == original and len(hw.firmware.sets) == writes


# ---- (3) idle start-up adoption of the laser yaml ----------------------------------------------------------------

def edited_laser():
    laser = fake_rig.laser_definition()
    rig = laser['devices']['rig']
    rig['control_settings']['axes']['piezo']['frequency_hz_per_unit'] = 91e3      # forbidden (calibration) live
    rig['absolute_conditions']['axes']['temperature']['activation_range'] = [-1., 1.]   # forbidden live
    rig['control_settings']['optical']['single_mode_max']['side_peak_height_max'] = .3    # quality
    return laser


def test_idle_start_adopts_the_edited_yaml_with_an_audit_record(tmp_path):
    clock, holder = Clock(), {}
    first = make_owner(tmp_path, clock, holder)
    assert first.startup['state'] == 'equal' and not first.startup['pending']
    first.close(hold=False)
    edited = edited_laser()
    owner = restart_with(tmp_path, clock, holder, laser=edited)
    assert owner.startup['state'] == 'adopted' and owner.startup['pending'] is False
    assert owner.laser == edited and digest(owner.device) == digest(edited['devices']['rig'])
    assert owner.policies()['piezo'].frequency_hz_per_unit == 91e3
    assert owner.policy_authority.snapshot() == edited
    record = audit_rows(owner, 'policy_adopted_at_start')[0]
    assert sorted(record['changed_paths']) == ['devices.rig.absolute_conditions.axes.temperature.activation_range',
                                               'devices.rig.control_settings.axes.piezo.frequency_hz_per_unit',
                                               'devices.rig.control_settings.optical.single_mode_max.side_peak_height_max']
    assert record['classes']['devices.rig.control_settings.axes.piezo.frequency_hz_per_unit'] == 'forbidden'
    assert record['dependencies_changed'] is True and record['effective_revision'] == digest(edited['devices']['rig'])
    assert record['saved_revision'] == digest(fake_rig.laser_definition()['devices']['rig'])
    assert 'hw' not in holder                                           # no hardware was opened for it
    describe = owner.dispatch(dict(action='describe'))
    assert describe['policy_startup']['state'] == 'adopted' and describe['revision'] == digest(edited['devices']['rig'])
    # the journal is the effective record: the next start on the same yaml sees no difference, a claim works
    owner.close(hold=False)
    again = restart_with(tmp_path, clock, holder, laser=edited)
    assert again.startup['state'] == 'equal' and again.laser == edited
    assert again.dispatch(dict(action='claim', revision=digest(again.device), owner='test'))['lease']
    assert again.policies()['piezo'].frequency_hz_per_unit == 91e3


def test_start_with_a_persisted_active_lock_refuses_a_differing_yaml_precisely(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    shutdown(owner)
    original = json.loads(owner.state_path.read_text())
    writes = len(hw.firmware.sets)
    opened = []
    def factory(device, adopt_leases=None):
        opened.append(device)
        return Rig(device, clock, previous=hw, adopt_leases=adopt_leases)
    with pytest.raises(StaleContext) as exc:
        ECDLOwner(edited_laser(), audit_path=tmp_path / 'audit.jsonl', factory=factory, clock=clock)
    message = str(exc.value)
    assert 'active lock' in message and lease in message and 'piezo.frequency_hz_per_unit (forbidden)' in message
    assert 'restore the yaml to the saved policy' in message and 'settings_adopt' in message
    assert opened == []                                                 # refused before any rig I/O
    assert json.loads(owner.state_path.read_text()) == original and len(hw.firmware.sets) == writes
    # the yaml restored to the saved policy: the lock is adopted unchanged, as before
    adopted = restart_with(tmp_path, clock, holder)
    assert adopted.adoption['state'] == 'adopted' and adopted.fault is None and adopted.startup['state'] == 'equal'
    assert len(hw.firmware.sets) == writes


def test_start_with_a_quarantine_refuses_a_differing_yaml_and_the_release_path_adopts_it_later(rig, tmp_path):
    owner, holder, clock, lease = rig
    hw = holder['hw']
    lock(owner, hw)
    shutdown(owner)
    hw.firmware.cfg['pid_multiplier'] = 0
    refused = restart(owner, tmp_path, clock, holder)
    assert refused.quarantine_path.exists()
    with pytest.raises(StaleContext, match='quarantine'):
        restart_with(tmp_path, clock, holder, laser=edited_laser())
    # the review: the human clears the quarantine and the retained lease, then the idle start adopts the yaml
    refused.quarantine_path.unlink()
    refused.clear_state()
    owner2 = restart_with(tmp_path, clock, holder, laser=edited_laser())
    assert owner2.startup['state'] == 'adopted' and owner2.fault is None and owner2.lease is None
