"""Real driver + Instrument with a formula-based console; no hardware or network."""
from copy import deepcopy
import re
import threading

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_schema import digest
from ldcon.drivers.dlcpro import DLCPro, UncertainDLCWrite, parse_atom
from ldcon.owners.dlcpro_ff_adjustment import adjust_factor, FFAdjustmentError
from ldcon.owners.dlcpro_owner import Instrument


class FormulaConsole:
    def __init__(self):
        self.lock = threading.RLock()
        self.pzt, self.tec, self.bias, self.factor = 53.8, 22.46, 80.263, -.16
        self.reference, self.residual, self.measured_error = 69.5, .0022, -.02
        self.ff, self.scan, self.tec_fault = True, False, False
        self.arc = dict(pc=False, cc=False)
        self.auto_preserve = False
        self.writes, self.snapshots = [], 0
        self.fail_write = self.fail_snapshot = None
        self.after_factor = None
        self.bad_compensation = False

    @property
    def total(self):
        return self.bias + (self.factor*(self.pzt-self.reference) if self.ff else 0.) + self.residual

    def command(self, expression, *, writing=False):
        match = re.fullmatch(r"\(param-(ref|set!) '([a-z0-9:-]+)(?: (.+))?\)", expression)
        assert match, expression
        operation, path, raw = match.groups()
        if operation == 'set!':
            assert writing
            value = parse_atom(raw)
            self.writes.append((path, value))
            if path.endswith('cc:feedforward-factor'):
                previous = self.total
                self.factor = value
                if self.auto_preserve:
                    self.bias = previous-self.factor*(self.pzt-self.reference)-self.residual
                if self.after_factor:
                    self.after_factor(self)
            elif path.endswith('cc:current-set'):
                if not self.bad_compensation:
                    self.bias = value-self.factor*(self.pzt-self.reference)-self.residual
            else:
                raise AssertionError('Unapproved mutation: '+path)
            if len(self.writes) == self.fail_write:
                raise UncertainDLCWrite('Synthetic post-execution timeout')
            return '0'
        assert not writing
        if path.endswith('pc:voltage-set'):
            self.snapshots += 1
            if self.snapshots == self.fail_snapshot:
                raise TimeoutError('Synthetic snapshot read failure')
        values = {
            'pc:voltage-set': self.pzt, 'pc:voltage-act': self.pzt,
            'tc:temp-set': self.tec, 'tc:temp-act': self.tec,
            'cc:current-offset': self.bias, 'cc:current-act': self.total+self.measured_error,
            'cc:current-set': self.total, 'cc:feedforward-enabled': self.ff,
            'cc:feedforward-factor': self.factor, 'tc:fault': self.tec_fault,
            'cc:emission': True, 'pc:enabled': True, 'tc:enabled': True,
            'pc:external-input:enabled': self.arc['pc'],
            'cc:external-input:enabled': self.arc['cc'],
        }
        if path == 'laser1:scan:enabled':
            value = self.scan
        elif path.startswith('laser1:dl:') and path[len('laser1:dl:'):] in values:
            value = values[path[len('laser1:dl:'):]]
        elif path in ('laser1:dl:pc:voltage-out', 'laser1:feedforward-master'):
            return 'Error: -3 unavailable'
        else:
            raise AssertionError('Unexpected read: '+path)
        return ('#t' if value else '#f') if type(value) is bool else repr(value)


@pytest.fixture
def rig():
    wire = FormulaConsole()
    axes = {'piezo': ([10., 90.], 'V'), 'temperature': ([18., 26.], 'degC'),
            'current': ([72., 88.], 'mA')}
    hardware = dict(axes={k: dict(bounds=v[0]) for k, v in axes.items()},
        channels={'1': dict(laser_id='test', output_filters_verified=True,
                            ff_reference_v=69.5, ff_delta_verified=True)})
    policy = dict(axes={k: dict(bounds=v[0], unit=v[1], frequency_hz_per_unit=1.,
        readback_tolerance=1e-6, settle_s=1., update_interval_s=.1) for k, v in axes.items()},
        ff_mode='managed_reference', ff_enabled=True, ff_factor_ma_per_v=-.16,
        ff_factor_bounds_ma_per_v=[-.5, 0.], max_sample_age_s=.2,
        measurement_timeout_s=15., client_timeout_s=2., quality_limits={})
    events = []
    instrument = Instrument(DLCPro(wire), hardware, audit=events.append)
    lease = instrument.claim(1, 'test', policy)
    return wire, instrument, lease, events


def run(rig, factor=-.155, **kwargs):
    _, instrument, lease, _ = rig
    return adjust_factor(instrument, 1, lease, factor,
        **dict(reference_v=69.5, tolerance_ma=.01, expected_factor=-.16, **kwargs))


@pytest.mark.parametrize('auto', [False, True])
@pytest.mark.parametrize('residual', [0., .0022])
def test_verified_factor_and_conditional_compensation(rig, auto, residual):
    wire, instrument, lease, events = rig
    wire.auto_preserve, wire.residual = auto, residual
    total, bias, previous = wire.total, wire.bias, deepcopy(instrument.channels[1]['policy'])
    result = run(rig)
    assert wire.ff and wire.factor == -.155
    assert wire.total == pytest.approx(total, abs=1e-12)
    assert wire.bias == pytest.approx(bias-.005*(wire.pzt-69.5), abs=1e-12)
    assert [p for p, _ in wire.writes] == ['laser1:dl:cc:feedforward-factor'] + (
        [] if auto else ['laser1:dl:cc:current-set'])
    assert result['compensation_written'] is not auto
    assert result['before']['current']['total_setpoint'] == total
    assert result['intermediate'] and result['after']
    assert result['phase'] == 'complete' and result['policy_adopted']
    assert not result['outputs_uncertain'] and result['persisted'] is False
    assert instrument.channels[1]['lease'] == lease and not instrument.channels[1]['fault']
    expected = dict(previous, ff_factor_ma_per_v=-.155)
    assert instrument.channels[1]['policy'] == expected
    assert result['revision_after'] == instrument.channels[1]['revision'] == digest(expected)
    assert instrument.confirmed_outputs[1]['current'] == wire.bias
    assert events[-1]['event'] == 'ff_adjust_verified'
    # Persistence is the caller's responsibility; the receipt does not claim it.
    assert events[-1]['receipt']['persisted'] is False
    again = adjust_factor(instrument, 1, lease, -.16, reference_v=69.5,
                          tolerance_ma=.01, expected_factor=-.155)
    assert again['after']['current']['total_setpoint'] == pytest.approx(total)


def test_noop_is_read_only_and_does_not_claim_policy_adoption(rig):
    result = run(rig, -.16)
    assert not rig[0].writes and not result['physical_changed']
    assert not result['policy_adopted'] and result['revision_before'] == result['revision_after']
    assert result['effect']['behavior'] == 'unchanged'


@pytest.mark.parametrize('case', ['stale_expected', 'stale_revision', 'wrong_reference',
    'no_delta_proof', 'preserve_mode', 'ff_off', 'factor_mismatch', 'scan', 'arc_pc',
    'arc_cc', 'tec_fault', 'model_mismatch', 'nonfinite_measured', 'measured_outside',
    'factor_bounds', 'zero_tolerance', 'nan_reference', 'wrong_lease'])
def test_prewrite_rejections_do_not_mutate_or_fault(rig, case):
    wire, inst, lease, _ = rig
    state = inst.channels[1]
    kwargs = dict(reference_v=69.5, tolerance_ma=.01, expected_factor=-.16)
    factor = -.155
    if case == 'stale_expected': kwargs['expected_factor'] = -.15
    elif case == 'stale_revision': state['revision'] = 'stale'
    elif case == 'wrong_reference': kwargs['reference_v'] = 69.
    elif case == 'no_delta_proof': inst.hardware['channels']['1']['ff_delta_verified'] = False
    elif case == 'preserve_mode': state['policy']['ff_mode'] = 'preserve_existing'
    elif case == 'ff_off': wire.ff = False
    elif case == 'factor_mismatch': wire.factor = -.15
    elif case == 'scan': wire.scan = True
    elif case.startswith('arc_'): wire.arc[case[4:]] = True
    elif case == 'tec_fault': wire.tec_fault = True
    elif case == 'model_mismatch': wire.residual = .02
    elif case == 'nonfinite_measured': wire.measured_error = float('nan')
    elif case == 'measured_outside': wire.measured_error = 10.
    elif case == 'factor_bounds': factor = -.51
    elif case == 'zero_tolerance': kwargs['tolerance_ma'] = 0.
    elif case == 'nan_reference': kwargs['reference_v'] = float('nan')
    elif case == 'wrong_lease': lease = 'stale'
    with pytest.raises((ValueError, DeviceError)):
        adjust_factor(inst, 1, lease, factor, **kwargs)
    assert not wire.writes and not state['fault']


@pytest.mark.parametrize('case', ['fixed_total', 'compensated_bias', 'hold_padding'])
def test_entire_intermediate_envelope_checked_before_first_write(rig, case):
    wire, _, _, _ = rig
    if case == 'fixed_total':
        factor = -.5  # Fixed-bias intermediate exceeds 88; compensated bias is safe.
    elif case == 'compensated_bias':
        wire.pzt, wire.bias = 90., 86.
        factor = -.5  # Total is safe but restoring it would require bias above 88.
    else:
        wire.bias += 87.995-wire.total
        factor = -.159  # Central values fit; the declared hold uncertainty does not.
    with pytest.raises(ValueError, match='bounds'):
        run(rig, factor)
    assert not wire.writes


@pytest.mark.parametrize('stage,count', [('factor_write', 1), ('intermediate_read', 1),
    ('compensation_write', 2), ('final_read', 2), ('compensation_readback', 2)])
def test_uncertain_stage_retains_lease_old_policy_and_never_rolls_back(rig, stage, count):
    wire, inst, lease, _ = rig
    if stage == 'factor_write': wire.fail_write = 1
    elif stage == 'intermediate_read': wire.fail_snapshot = 2
    elif stage == 'compensation_write': wire.fail_write = 2
    elif stage == 'final_read': wire.fail_snapshot = 3
    else: wire.bad_compensation = True
    with pytest.raises(FFAdjustmentError) as caught:
        run(rig)
    assert len(wire.writes) == count
    assert inst.channels[1]['lease'] == lease and inst.channels[1]['fault']
    assert inst.channels[1]['policy']['ff_factor_ma_per_v'] == -.16
    receipt = caught.value.receipt
    assert receipt['phase'] == 'failed' and receipt['outputs_uncertain']
    assert receipt['before'] and not receipt['policy_adopted']
    with pytest.raises(DeviceError): run(rig)
    assert len(wire.writes) == count


@pytest.mark.parametrize('change', ['unknown_offset', 'pzt', 'tec', 'arc', 'ff_off', 'model'])
def test_unexpected_postfactor_state_is_retained_and_not_compensated(rig, change):
    wire, inst, _, _ = rig
    def mutate(w):
        if change == 'unknown_offset': w.bias += .1
        elif change == 'pzt': w.pzt += .1
        elif change == 'tec': w.tec += .01
        elif change == 'arc': w.arc['pc'] = True
        elif change == 'ff_off': w.ff = False
        else: w.residual = .1
    wire.after_factor = mutate
    with pytest.raises(FFAdjustmentError) as caught:
        run(rig)
    assert len(wire.writes) == 1 and inst.channels[1]['fault']
    assert caught.value.receipt['intermediate'] is not None
    assert not caught.value.receipt['compensation_written']


def test_failed_audit_after_verified_hardware_does_not_adopt_or_replay(rig):
    wire, inst, lease, events = rig
    def audit(event):
        if event['event'] == 'ff_adjust_verified': raise OSError('durable audit unavailable')
        events.append(event)
    inst.audit = audit
    with pytest.raises(FFAdjustmentError) as caught:
        run(rig)
    assert caught.value.receipt['after'] is not None
    assert len(wire.writes) == 2 and inst.channels[1]['lease'] == lease
    assert inst.channels[1]['policy']['ff_factor_ma_per_v'] == -.16
    assert inst.channels[1]['fault']
