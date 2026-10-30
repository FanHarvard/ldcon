"""Opt-in native timing verification through the real scalar driver/Instrument."""
from copy import deepcopy
import json
import re

import pytest

from ldcon.core.contracts import DeviceError, FrequencySample
from ldcon.drivers.dlcpro import DLCPro, UncertainDLCWrite, parse_atom
from ldcon.owners.dlcpro_owner import Instrument, LaserOwner
from ldcon.owners.ecdl_prelock import ModeObservation
from .test_dlcpro_ff_adjustment import FormulaConsole, rig as ff_rig
from .test_dlcpro_offline import Clock


class ScanConsole(FormulaConsole):
    def __init__(self):
        super().__init__()
        self.settings = dict(frequency=9.999942752959514, amplitude=5., offset=self.pzt,
            hold=False, unit='V', **{'output-channel': 50, 'signal-type': 1})
        self.actual_frequency = None
        self.actual_amplitude = None
        self.frequency_status = 0
        self.frequency_read_failure = False

    def command(self, expression, *, writing=False):
        match = re.fullmatch(r"\(param-(ref|set!) 'laser1:scan:([a-z-]+)(?: (.+))?\)", expression)
        if not match:
            return super().command(expression, writing=writing)
        operation, name, raw = match.groups()
        if operation == 'set!':
            assert writing
            value = parse_atom(raw)
            self.writes.append(('laser1:scan:'+name, value))
            if name == 'enabled':
                self.scan = value
            else:
                actual = self.actual_frequency if name == 'frequency' else self.actual_amplitude if name == 'amplitude' else None
                self.settings[name] = value if actual is None else actual
            return str(self.frequency_status if name == 'frequency' else 0)
        assert not writing
        if name == 'frequency' and self.frequency_read_failure:
            raise TimeoutError('Frequency readback unavailable')
        value = self.scan if name == 'enabled' else self.settings[name]
        if type(value) is bool:
            return '#t' if value else '#f'
        return json.dumps(value)


@pytest.fixture
def rig(ff_rig):
    hardware = deepcopy(ff_rig[1].hardware)
    hardware['channels']['1'].update(pzt_scan_output_channel=50,
                                     scan_frequency_bounds_hz=[.099, .101])
    policy = deepcopy(ff_rig[1].channels[1]['policy'])
    wire, events = ScanConsole(), []
    inst = Instrument(DLCPro(wire), hardware, audit=events.append)
    lease = inst.claim(1, 'test', policy)
    return wire, inst, lease, events


def scan(rig, frequency=.1):
    wire, inst, lease, _ = rig
    return inst.scan(1, lease, center=wire.pzt, amplitude=.2,
                     frequency=frequency, shape='triangle', enabled=True)


def enable_writes(wire):
    return [value for path, value in wire.writes if path == 'laser1:scan:enabled']


def test_legacy_default_remains_exact_and_no_config_default_is_inserted(rig):
    wire, inst, _, events = rig
    receipt = scan(rig)
    assert receipt['requested_frequency_hz'] == receipt['applied_frequency_hz'] == .1
    assert receipt['applied_period_s'] == 10.
    assert receipt['frequency_readback_tolerance_hz'] == 0.
    assert 'scan_frequency_readback_tolerance_hz' not in inst.hardware['channels']['1']
    assert enable_writes(wire) == [True]
    intent = next(r for r in events if r['event'] == 'write_intent' and r['parameter'].endswith(':frequency'))
    assert intent['readback_tolerance'] == 0.


def test_legacy_exact_zero_rejects_quantized_frequency_before_enable(rig):
    wire, inst, lease, _ = rig
    wire.actual_frequency = .0999995
    with pytest.raises(UncertainDLCWrite):
        scan(rig)
    assert not enable_writes(wire) and not wire.scan
    assert inst.channels[1]['fault'] and inst.channels[1]['lease'] == lease


def test_quantized_frequency_receipt_audit_pairing_and_timing_are_explicit(rig):
    wire, inst, _, events = rig
    inst.hardware['channels']['1']['scan_frequency_readback_tolerance_hz'] = .001
    wire.actual_frequency = .0999995
    receipt = scan(rig)
    assert receipt['requested_frequency_hz'] == .1
    assert receipt['applied_frequency_hz'] == wire.actual_frequency
    assert receipt['applied_period_s'] == 1/wire.actual_frequency
    assert receipt['frequency_readback_tolerance_hz'] == .001
    assert enable_writes(wire) == [True]
    pending = None
    for event in events:
        if event['event'] == 'write_intent':
            assert pending is None
            pending = event
        elif event['event'] == 'write_verified':
            assert pending is not None
            assert (pending['channel'], pending['parameter']) == (event['channel'], event['parameter'])
            assert event['requested_value'] == pending['value']
            assert event['readback_tolerance'] == pending['readback_tolerance']
            if type(pending['value']) is bool:
                assert event['value'] is pending['value']
            else:
                assert abs(event['value']-pending['value']) <= pending['readback_tolerance']
            pending = None
    assert pending is None
    frequency = next(r for r in events if r['event'] == 'write_verified' and r['parameter'].endswith(':frequency'))
    assert frequency['value'] != frequency['requested_value']


def test_actual_frequency_outside_study_range_is_not_enabled(rig):
    wire, inst, lease, events = rig
    inst.hardware['channels']['1']['scan_frequency_readback_tolerance_hz'] = .001
    wire.actual_frequency = .1012  # Within request + tolerance, beyond the admitted .101 maximum.
    with pytest.raises(DeviceError, match='outside admission'):
        scan(rig, .1005)
    assert not enable_writes(wire) and not wire.scan
    assert wire.writes[-1][0] == 'laser1:scan:frequency'
    assert inst.channels[1]['fault'] and inst.channels[1]['lease'] == lease
    assert events[-1]['event'] == 'scan_frequency_rejected'
    assert any(r['event'] == 'write_verified' and r['value'] == .1012 for r in events)


@pytest.mark.parametrize('failure', ['outside_tolerance', 'clipped', 'read_failure'])
def test_tolerance_never_accepts_clipping_or_uncertain_readback(rig, failure):
    wire, inst, lease, events = rig
    inst.hardware['channels']['1']['scan_frequency_readback_tolerance_hz'] = .001
    if failure == 'outside_tolerance': wire.actual_frequency = .0989
    elif failure == 'clipped': wire.frequency_status = 2
    else: wire.frequency_read_failure = True
    with pytest.raises(UncertainDLCWrite):
        scan(rig)
    assert not enable_writes(wire) and not wire.scan
    assert inst.channels[1]['fault'] and inst.channels[1]['lease'] == lease
    assert not any(r['event'] == 'write_verified' and r['parameter'].endswith(':frequency') for r in events)
    writes = list(wire.writes)
    with pytest.raises(DeviceError): scan(rig)
    assert wire.writes == writes


def test_frequency_tolerance_does_not_relax_amplitude_verification(rig):
    wire, inst, _, _ = rig
    inst.hardware['channels']['1']['scan_frequency_readback_tolerance_hz'] = .001
    wire.actual_amplitude = .19999
    with pytest.raises(UncertainDLCWrite):
        scan(rig)
    assert not enable_writes(wire)


@pytest.mark.parametrize('value', [-.001, float('nan'), float('inf'), True, None])
def test_invalid_tolerance_rejected_during_offline_configuration(rig, value):
    wire, inst, _, _ = rig
    config = deepcopy(inst.hardware)
    config['channels']['1']['scan_frequency_readback_tolerance_hz'] = value
    with pytest.raises(ValueError):
        Instrument(None, config)
    assert not wire.writes


def test_invalid_generic_write_tolerance_is_rejected_before_intent(rig):
    wire, inst, lease, events = rig
    with pytest.raises(ValueError):
        inst._write(1, lease, 'laser1:scan:frequency', .1, -.001)
    assert not wire.writes and not any(r['event'] == 'write_intent' for r in events)


def owner_for(rig):
    _, inst, lease, events = rig
    policy = deepcopy(inst.channels[1]['policy'])
    inst.release(1, lease)
    clock = Clock()
    class Source:
        def read_mode(self):
            sample = FrequencySample(1, 3e14, clock(), clock(), 'valid', 'synthetic')
            return ModeObservation(sample, True, 'branch', True, .1, False, {})
    owner = LaserOwner(inst, 1, 'test', policy, Source(), clock=clock, audit=events.append)
    spec = dict(kp=.45, ki_per_s=1., integral_limit_v=10., capture_hz=1e8,
        tolerance_hz=1e7, tec_gain=.1, tec_period_s=10., pzt_center_v=53.8,
        period_s=10., span_hz=.2, shape='triangle', center_lock=False,
        max_gap_s=.2, boundary_tolerance_s=.1)
    return owner, spec


def test_owner_cycle_uses_applied_period_and_preserves_requested_spec(rig):
    wire, inst, _, _ = rig
    wire.actual_frequency = .1005
    inst.hardware['channels']['1']['scan_frequency_readback_tolerance_hz'] = .001
    owner, spec = owner_for(rig)
    owner.start_scan(3e14, spec)
    assert owner.cycle.period == 1/.1005 and owner.cycle.period != spec['period_s']
    assert owner.scan_spec['period_s'] == 10.
    assert owner.snapshot()['scan_receipt']['applied_frequency_hz'] == .1005
    assert enable_writes(wire) == [True]


def test_owner_rejects_gap_invalid_at_tolerated_frequency_before_writes(rig):
    wire, inst, _, _ = rig
    inst.hardware['channels']['1']['scan_frequency_readback_tolerance_hz'] = .001
    owner, spec = owner_for(rig)
    spec['max_gap_s'] = 4.99  # Passes requested 10 s period, fails fastest accepted period.
    with pytest.raises(ValueError, match='tolerated frequency'):
        owner.start_scan(3e14, spec)
    assert not wire.writes and owner.mode == 'idle'
