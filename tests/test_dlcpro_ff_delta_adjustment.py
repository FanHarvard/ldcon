"""Qualified-delta transaction through the actual driver and formula console."""
from copy import deepcopy
import json

import pytest

from ldcon.core.contracts import DeviceError
from ldcon.drivers.dlcpro import DLCPro
from ldcon.owners.dlcpro_ff_adjustment import FFAdjustmentError
from ldcon.owners.dlcpro_ff_delta_adjustment import adjust_factor_qualified_delta
from .test_dlcpro_ff_adjustment import FormulaConsole, rig


class QualifiedConsole(FormulaConsole):
    def __init__(self):
        super().__init__()
        self.identity = dict(serial_number='test-chassis', firmware_version='1.9.0.test')
        self.endpoints = [-1., 140.]

    def command(self, expression, *, writing=False):
        values = {'serial-number': self.identity['serial_number'],
                  'fw-ver': self.identity['firmware_version'],
                  'laser1:dl:pc:voltage-min': self.endpoints[0],
                  'laser1:dl:pc:voltage-max': self.endpoints[1]}
        for path, value in values.items():
            if expression == f"(param-ref '{path})":
                assert not writing
                return json.dumps(value)
        return super().command(expression, writing=writing)


@pytest.fixture
def qualified(rig):
    _, inst, lease, events = rig
    wire = QualifiedConsole()
    inst.driver = DLCPro(wire)
    evidence = dict(identity=deepcopy(wire.identity), pzt_hardware_bounds_v=list(wire.endpoints),
                    pzt_delta_qualification=dict(verified=True, channel=1, source='test-proof.json'))
    anchor = dict(piezo_v=wire.pzt, temperature_c=wire.tec, total_current_ma=wire.total)
    return wire, inst, lease, events, evidence, anchor


def run_qualified(qualified, factor=-.14, *, expected=-.16):
    _, inst, lease, _, evidence, anchor = qualified
    return adjust_factor_qualified_delta(inst, 1, lease, factor, evidence=evidence,
        anchor=anchor, tolerance_ma=.01, expected_factor=expected)


@pytest.mark.parametrize('auto', [False, True])
@pytest.mark.parametrize('reference,residual', [(68.7, .03), (71., -.04)])
def test_qualified_delta_holds_anchor_without_assuming_hidden_reference(qualified, auto, reference, residual):
    wire, inst, lease, events, evidence, anchor = qualified
    wire.auto_preserve, wire.reference, wire.residual = auto, reference, residual
    anchor['total_current_ma'] = wire.total
    result = run_qualified(qualified)
    assert wire.total == pytest.approx(anchor['total_current_ma'], abs=1e-12)
    assert result['effect']['behavior'] == ('total_preserved' if auto else 'fixed_bias_compensated')
    assert abs(result['model_residual_ma']['before']) > .01
    assert result['admitted']['drive_bound_v'] == 141.
    assert result['admitted']['total_transition_ma'][1]-anchor['total_current_ma'] > .02*141.
    assert result['evidence'] == evidence and result['identity_before'] == result['identity_after']
    assert result['persisted'] is False and result['policy_adopted']
    assert not inst.channels[1]['fault'] and inst.channels[1]['lease'] == lease
    assert events[-1]['receipt']['method'] == 'qualified_delta'
    returned = run_qualified(qualified, -.16, expected=-.14)
    assert returned['anchor'] == anchor and wire.total == pytest.approx(anchor['total_current_ma'])
    assert all(p in ('laser1:dl:cc:feedforward-factor', 'laser1:dl:cc:current-set') for p, _ in wire.writes)


def test_qualified_coefficient_dependent_residual_is_diagnostic(qualified):
    wire, _, _, _, _, _ = qualified
    wire.auto_preserve = True
    def change_residual(w):
        w.residual += .04
        w.bias -= .04  # Observed firmware preservation with a different intercept.
    wire.after_factor = change_residual
    result = run_qualified(qualified)
    assert result['model_residual_ma']['after'] == pytest.approx(.0422)
    assert result['effect']['total_delta_ma'] == pytest.approx(0.)
    assert len(wire.writes) == 1 and not result['compensation_written']


def test_qualified_noop_and_no_effect_do_not_claim_physical_coefficient_response(qualified):
    wire, _, _, _, _, _ = qualified
    result = run_qualified(qualified, -.16)
    assert not wire.writes and not result['policy_adopted']
    assert result['effect']['behavior'] == 'unchanged'
    previous = wire.total
    def no_effect(w):
        w.residual += previous-w.total  # Register changes but observable contribution does not.
    wire.after_factor = no_effect
    result = run_qualified(qualified)
    assert result['effect']['behavior'] == 'no_observable_effect'
    assert result['effect']['response_observed'] is False
    assert len(wire.writes) == 1 and not result['compensation_written']


@pytest.mark.parametrize('case', ['missing_proof', 'wrong_channel', 'empty_source', 'identity',
    'firmware', 'endpoints', 'bad_endpoints', 'missing_delta', 'stale_policy', 'stale_revision',
    'anchor_current', 'anchor_pzt', 'anchor_tec', 'preserve_mode', 'scan', 'arc', 'ff_off',
    'out_of_bounds', 'nonfinite', 'invalid_hold'])
def test_qualified_prewrite_rejection_never_commands(qualified, case):
    wire, inst, lease, _, evidence, anchor = qualified
    kwargs = dict(evidence=evidence, anchor=anchor, tolerance_ma=.01, expected_factor=-.16)
    if case == 'missing_proof': evidence['pzt_delta_qualification']['verified'] = False
    elif case == 'wrong_channel': evidence['pzt_delta_qualification']['channel'] = 2
    elif case == 'empty_source': evidence['pzt_delta_qualification']['source'] = ''
    elif case == 'identity': wire.identity['serial_number'] = 'other-chassis'
    elif case == 'firmware': wire.identity['firmware_version'] = 'other-firmware'
    elif case == 'endpoints': wire.endpoints = [-1., 150.]
    elif case == 'bad_endpoints': evidence['pzt_hardware_bounds_v'] = [140., -1.]
    elif case == 'missing_delta': inst.hardware['channels']['1']['ff_delta_verified'] = False
    elif case == 'stale_policy': kwargs['expected_factor'] = -.15
    elif case == 'stale_revision': inst.channels[1]['revision'] = 'stale'
    elif case == 'anchor_current': wire.bias += .02
    elif case == 'anchor_pzt': wire.pzt += .01
    elif case == 'anchor_tec': wire.tec += .01
    elif case == 'preserve_mode': inst.channels[1]['policy']['ff_mode'] = 'preserve_existing'
    elif case == 'scan': wire.scan = True
    elif case == 'arc': wire.arc['cc'] = True
    elif case == 'ff_off': wire.ff = False
    elif case == 'out_of_bounds': wire.measured_error = 10.
    elif case == 'nonfinite': anchor['total_current_ma'] = float('nan')
    else: kwargs['tolerance_ma'] = 0.
    with pytest.raises((ValueError, DeviceError)):
        adjust_factor_qualified_delta(inst, 1, lease, -.14, **kwargs)
    assert not wire.writes and not inst.channels[1]['fault']


@pytest.mark.parametrize('case', ['total_edge', 'bias_edge', 'hold_margin'])
def test_qualified_conservative_envelope_includes_all_intermediate_bounds(qualified, case):
    wire, _, _, _, _, anchor = qualified
    if case == 'total_edge': wire.bias += 86.-wire.total
    elif case == 'bias_edge': wire.reference, wire.bias = wire.pzt, 73.
    else: wire.bias += 87.995-wire.total
    anchor['total_current_ma'] = wire.total
    with pytest.raises(ValueError, match='bounds'):
        run_qualified(qualified, -.14 if case != 'hold_margin' else -.159999)
    assert not wire.writes


@pytest.mark.parametrize('stage,count', [('factor', 1), ('intermediate', 1),
    ('compensation', 2), ('final', 2), ('bad_compensation', 2), ('audit', 2)])
def test_qualified_uncertain_write_or_read_never_rolls_back(qualified, stage, count):
    wire, inst, lease, events, _, _ = qualified
    if stage == 'factor': wire.fail_write = 1
    elif stage == 'intermediate': wire.fail_snapshot = 2
    elif stage == 'compensation': wire.fail_write = 2
    elif stage == 'final': wire.fail_snapshot = 3
    elif stage == 'bad_compensation': wire.bad_compensation = True
    else:
        def audit(event):
            if event['event'] == 'ff_adjust_verified': raise OSError('durable audit unavailable')
            events.append(event)
        inst.audit = audit
    with pytest.raises(FFAdjustmentError) as caught:
        run_qualified(qualified)
    assert len(wire.writes) == count
    assert inst.channels[1]['fault'] and inst.channels[1]['lease'] == lease
    assert inst.channels[1]['policy']['ff_factor_ma_per_v'] == -.16
    assert caught.value.receipt['outputs_uncertain'] and not caught.value.receipt['policy_adopted']
    with pytest.raises(DeviceError): run_qualified(qualified, -.16)
    assert len(wire.writes) == count


@pytest.mark.parametrize('change', ['unknown_response', 'outside_total_envelope',
    'outside_bias_envelope', 'pzt', 'tec', 'arc', 'ff_off', 'identity'])
def test_qualified_unrecognized_response_never_compensates(qualified, change):
    wire, inst, _, _, _, _ = qualified
    def mutate(w):
        if change == 'unknown_response': w.bias += .1
        elif change == 'outside_total_envelope': w.residual += 3.5
        elif change == 'outside_bias_envelope':
            w.auto_preserve = True
            w.bias += 3.5
            w.residual -= 3.5 + .02*(w.pzt-w.reference)
        elif change == 'pzt': w.pzt += .1
        elif change == 'tec': w.tec += .01
        elif change == 'arc': w.arc['pc'] = True
        elif change == 'ff_off': w.ff = False
        else:
            w.identity['firmware_version'] = 'changed'
    wire.after_factor = mutate
    with pytest.raises(FFAdjustmentError) as caught:
        run_qualified(qualified)
    assert len(wire.writes) == 1 and inst.channels[1]['fault']
    assert caught.value.receipt['intermediate'] is not None
    assert not caught.value.receipt['compensation_written']
