"""Finite FF commissioning transaction on an exclusively held Instrument.

The caller stops the live owner, holds the chassis lock, pins identity/firmware,
and supplies independently commissioned reference/delta evidence and tolerances.
This is not a live-owner RPC, persistence mechanism, master-selector probe, or
permission to infer a reference. FF remains ON throughout. Two verified writes
are sequential, not an atomic or necessarily bumpless physical transition.
"""
from copy import deepcopy

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_schema import digest
from ldcon.drivers.dlcpro import UncertainDLCWrite
from ldcon.owners.dlcpro_ff import finite
from ldcon.owners.dlcpro_owner import inside


class FFAdjustmentError(UncertainDLCWrite):
    """Post-write failure; the attached receipt and retained lease need review."""
    def __init__(self, receipt):
        self.receipt = deepcopy(receipt)
        super().__init__('FF adjustment not verified; lease retained, no rollback')


def adjust_factor(instrument, channel, lease, factor, *, reference_v,
                  tolerance_ma, expected_factor):
    """Change an enabled FF coefficient and restore the original total setpoint.

    On verified success, adopt only the Instrument's in-memory policy/revision.
    The caller must persist the receipt and reconcile its configuration before
    releasing/restarting anything. Any post-write error faults the channel and
    carries the partial receipt; no compensating write follows uncertain I/O.
    ``tolerance_ma`` bounds the commissioned model/hold error, not current-act
    accuracy. Measured current is checked independently against absolute bounds.
    """
    factor, reference_v = finite(factor), finite(reference_v)
    expected_factor, tolerance_ma = finite(expected_factor), finite(tolerance_ma)
    if tolerance_ma <= 0:
        raise ValueError('Positive commissioned current tolerance required')
    with instrument.mutex, instrument.driver.control_transaction():
        state = instrument._state(channel, lease)
        policy = deepcopy(state['policy'])
        hardware = instrument.hardware['channels'][str(channel)]
        if policy.get('ff_mode') != 'managed_reference':
            raise ValueError('FF adjustment requires a commissioned managed reference')
        if (finite(hardware['ff_reference_v']) != reference_v
                or hardware.get('ff_delta_verified') is not True):
            raise ValueError('Reference/delta evidence differs from commissioned hardware')
        if (policy['ff_enabled'] is not True
                or policy['ff_factor_ma_per_v'] != expected_factor
                or state['revision'] != digest(policy)):
            raise ValueError('Stale FF policy or expected coefficient')
        inside(factor, policy['ff_factor_bounds_ma_per_v'])

        def bounded(axis, value):
            inside(value, policy['axes'][axis]['bounds'])
            inside(value, instrument.hardware['axes'][axis]['bounds'])

        def snapshot(wanted, origin=None, capture=None):
            value = instrument.snapshot(channel, lease)
            if capture is not None:
                receipt[capture] = value
            if instrument.driver.read(f'laser{channel}:scan:enabled') is not False:
                raise DeviceError('Native scan must be disabled')
            value['arc_enabled'] = {axis: instrument.driver.read(
                instrument.driver.path(channel, f'{axis}:external-input:enabled'))
                for axis in ('pc', 'cc')}
            if any(v is not False for v in value['arc_enabled'].values()):
                raise DeviceError('PZT/current ARC must be disabled')
            if (value['ff_enabled'] is not True
                    or value['ff_factor_ma_per_v'] != wanted
                    or value['tec_fault'] is not False):
                raise DeviceError('FF/TEC readback differs from the admitted context')
            for axis in policy['axes']:
                bounded(axis, value[axis]['setpoint'])
                bounded(axis, value[axis]['measured'])
            bounded('current', value['current']['total_setpoint'])
            if origin is not None:
                for axis in ('piezo', 'temperature'):
                    if abs(value[axis]['setpoint']-origin[axis]['setpoint']) > policy['axes'][axis]['readback_tolerance']:
                        raise DeviceError('PZT/TEC changed during FF adjustment')
            calculated = value['current']['setpoint'] + wanted * (value['piezo']['setpoint']-reference_v)
            if abs(value['current']['total_setpoint']-calculated) > tolerance_ma:
                raise DeviceError('Readback does not satisfy the commissioned FF model')
            return value

        before = snapshot(expected_factor)
        total, bias = before['current']['total_setpoint'], before['current']['setpoint']
        displacement = before['piezo']['setpoint']-reference_v
        receipt = dict(before=before, intermediate=None, after=None,
            reference_v=reference_v, tolerance_ma=tolerance_ma,
            expected_factor=expected_factor, requested_factor=factor,
            revision_before=state['revision'], revision_after=state['revision'],
            phase='preflight', physical_changed=False, outputs_uncertain=False,
            policy_adopted=False, persisted=False, compensation_written=False)
        if factor == expected_factor:
            receipt.update(intermediate=deepcopy(before), after=deepcopy(before),
                phase='complete', effect=dict(behavior='unchanged', offset_delta_ma=0., total_delta_ma=0.))
            return receipt

        # Bound both firmware behaviors before changing the coefficient: fixed
        # offset, or automatic preservation of total current. Include model and
        # hold tolerance rather than silently widening the absolute limits.
        fixed_total = bias + factor * displacement
        compensated_bias = total - factor * displacement
        motion_margin = abs(factor)*policy['axes']['piezo']['readback_tolerance']
        fixed_margin = tolerance_ma + motion_margin + policy['axes']['current']['readback_tolerance']
        bias_margin = 2*tolerance_ma + motion_margin
        admitted = dict(fixed_bias_total_ma=[fixed_total-fixed_margin, fixed_total+fixed_margin],
            compensated_bias_ma=[compensated_bias-bias_margin, compensated_bias+bias_margin],
            held_total_ma=[total-tolerance_ma, total+tolerance_ma])
        for bounds in admitted.values():
            for value in bounds:
                bounded('current', value)
        receipt['admitted'] = admitted
        instrument.audit(dict(event='ff_adjust_preflight', channel=channel, receipt=deepcopy(receipt)))
        try:
            receipt.update(phase='factor_write', physical_changed=None, outputs_uncertain=True)
            instrument._write(channel, lease, instrument.driver.path(channel, 'cc:feedforward-factor'), factor)
            receipt['phase'] = 'intermediate_readback'
            intermediate = snapshot(factor, before, 'intermediate')
            current = intermediate['current']
            held = abs(current['total_setpoint']-total) <= tolerance_ma
            fixed = abs(current['setpoint']-bias) <= policy['axes']['current']['readback_tolerance']
            if not (held or fixed):
                raise DeviceError('Unknown coefficient-write response; no automatic compensation')
            if not admitted['compensated_bias_ma'][0] <= current['setpoint'] <= admitted['compensated_bias_ma'][1] and not fixed:
                raise DeviceError('Automatic offset response exceeds the admitted envelope')
            if not held:
                receipt['phase'] = 'total_current_compensation'
                # Documented current-set setter adjusts current-offset. It uses
                # the fresh known post-factor state, never an assumed old bias.
                instrument._write(channel, lease, instrument.driver.path(channel, 'cc:current-set'),
                                  total, policy['axes']['current']['readback_tolerance'])
                receipt['compensation_written'] = True
            receipt['phase'] = 'final_readback'
            after = snapshot(factor, before, 'after')
            if (abs(after['current']['total_setpoint']-total) > tolerance_ma
                    or not admitted['compensated_bias_ma'][0] <= after['current']['setpoint'] <= admitted['compensated_bias_ma'][1]):
                raise DeviceError('Total current/offset compensation not verified')
            policy['ff_factor_ma_per_v'] = factor
            receipt.update(phase='complete', physical_changed=True, outputs_uncertain=False,
                revision_after=digest(policy), policy_adopted=True,
                effect=dict(behavior='total_preserved' if held else 'fixed_bias_compensated',
                    offset_delta_ma=after['current']['setpoint']-bias,
                    total_delta_ma=after['current']['total_setpoint']-total))
            instrument.audit(dict(event='ff_adjust_verified', channel=channel, receipt=deepcopy(receipt)))
            state.update(policy=policy, revision=receipt['revision_after'])
            return receipt
        except BaseException as error:
            receipt.update(outputs_uncertain=True, policy_adopted=False,
                failed_phase=receipt['phase'], phase='failed',
                error=dict(type=type(error).__name__, reason=str(error)))
            state['fault'] = 'FF adjustment outcome requires inspection; lease retained'
            try:
                instrument.audit(dict(event='ff_adjust_uncertain', channel=channel, receipt=deepcopy(receipt)))
            except BaseException as audit_error:
                receipt['audit_error'] = type(audit_error).__name__
            raise FFAdjustmentError(receipt) from error
