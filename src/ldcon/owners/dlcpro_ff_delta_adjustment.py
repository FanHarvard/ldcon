"""Qualified-delta FF trials on one exclusively held maintenance Instrument.

This finite transaction does not expose a live-owner RPC or change ordinary FF
policy. Physical linkage evidence is commissioned separately; no master selector
or hidden reference is inferred, and unknown writes are never replayed.
"""
from copy import deepcopy

from ldcon.core.contracts import DeviceError
from ldcon.core.interface_schema import digest
from ldcon.owners.dlcpro_ff import finite
from ldcon.owners.dlcpro_ff_adjustment import FFAdjustmentError
from ldcon.owners.dlcpro_owner import inside


def adjust_factor_qualified_delta(instrument, channel, lease, factor, *, evidence,
                                  anchor, tolerance_ma, expected_factor):
    """Finite, exclusive coefficient trial using a qualified PZT-delta bound.

    ``evidence`` pins identity, independently read ``pzt_hardware_bounds_v`` and
    ``pzt_delta_qualification`` (verified, channel, durable source). Its physical
    contract is that a coefficient increment acts on a PZT-derived drive no
    larger than the full hardware span. Endpoint reads alone do not prove that
    contract or identify the inaccessible FF master. The caller commissions it.

    ``anchor`` retains the study's original piezo_v, temperature_c and
    total_current_ma across trials, including returns. This function neither
    infers a reference nor qualifies a subsequent scan. The configured reference
    supplies diagnostics only; current hold and absolute limits remain enforced.
    Success adopts only the in-memory factor; persistence belongs to the caller.
    """
    factor, expected_factor = finite(factor), finite(expected_factor)
    tolerance_ma = finite(tolerance_ma)
    if tolerance_ma <= 0:
        raise ValueError('Positive commissioned current hold tolerance required')
    evidence, anchor = deepcopy(evidence), deepcopy(anchor)
    identity = evidence['identity']
    if any(not isinstance(identity.get(k), str) or not identity[k].strip()
           for k in ('serial_number', 'firmware_version')):
        raise ValueError('Pinned chassis and firmware evidence required')
    endpoints = [finite(v) for v in evidence['pzt_hardware_bounds_v']]
    if len(endpoints) != 2 or endpoints[0] >= endpoints[1]:
        raise ValueError('Ordered PZT hardware endpoints required')
    qualification = evidence['pzt_delta_qualification']
    if (qualification.get('verified') is not True
            or type(qualification.get('channel')) is not int
            or qualification['channel'] != channel
            or not isinstance(qualification.get('source'), str)
            or not qualification['source'].strip()):
        raise ValueError('Channel-specific PZT-delta qualification evidence required')
    for key in ('piezo_v', 'temperature_c', 'total_current_ma'):
        anchor[key] = finite(anchor[key])
    with instrument.mutex, instrument.driver.control_transaction():
        state = instrument._state(channel, lease)
        policy = deepcopy(state['policy'])
        hardware = instrument.hardware['channels'][str(channel)]
        if (policy.get('ff_mode') != 'managed_reference'
                or hardware.get('ff_delta_verified') is not True):
            raise ValueError('Explicit commissioned maintenance Instrument required')
        if (policy['ff_enabled'] is not True
                or policy['ff_factor_ma_per_v'] != expected_factor
                or state['revision'] != digest(policy)):
            raise ValueError('Stale FF policy or expected coefficient')
        inside(factor, policy['ff_factor_bounds_ma_per_v'])
        reference = finite(hardware['ff_reference_v'])  # Diagnostic, never inferred.
        read_identity = instrument.driver.verify_identity(
            identity['serial_number'], identity['firmware_version'])
        read_endpoints = [instrument.driver.read(instrument.driver.path(channel, name))
                          for name in ('pc:voltage-min', 'pc:voltage-max')]
        if read_endpoints != endpoints:
            raise DeviceError('PZT hardware bounds differ from qualification evidence')

        def bounded(axis, value):
            inside(value, policy['axes'][axis]['bounds'])
            inside(value, instrument.hardware['axes'][axis]['bounds'])

        for axis, key in (('piezo', 'piezo_v'), ('temperature', 'temperature_c'),
                          ('current', 'total_current_ma')):
            bounded(axis, anchor[key])
        inside(anchor['piezo_v'], endpoints)
        receipt = dict(method='qualified_delta', evidence=evidence, anchor=anchor,
            identity_before=read_identity, pzt_hardware_bounds_v=read_endpoints,
            diagnostic_reference_v=reference, model_residual_ma={},
            tolerance_ma=tolerance_ma, expected_factor=expected_factor,
            requested_factor=factor, before=None, intermediate=None, after=None,
            revision_before=state['revision'], revision_after=state['revision'],
            phase='preflight', physical_changed=False, outputs_uncertain=False,
            policy_adopted=False, persisted=False, compensation_written=False)

        def snapshot(wanted, capture):
            value = instrument.snapshot(channel, lease)
            receipt[capture] = value
            value['scan_enabled'] = instrument.driver.read(f'laser{channel}:scan:enabled')
            value['arc_enabled'] = {axis: instrument.driver.read(
                instrument.driver.path(channel, f'{axis}:external-input:enabled'))
                for axis in ('pc', 'cc')}
            if value['scan_enabled'] is not False or any(
                    v is not False for v in value['arc_enabled'].values()):
                raise DeviceError('Native scan and PZT/current ARC must be disabled')
            if (value['ff_enabled'] is not True or value['ff_factor_ma_per_v'] != wanted
                    or value['tec_fault'] is not False):
                raise DeviceError('FF/TEC readback differs from the admitted context')
            for axis in policy['axes']:
                bounded(axis, value[axis]['setpoint'])
                bounded(axis, value[axis]['measured'])
            bounded('current', value['current']['total_setpoint'])
            inside(value['piezo']['setpoint'], endpoints)
            for axis, key in (('piezo', 'piezo_v'), ('temperature', 'temperature_c')):
                if abs(value[axis]['setpoint']-anchor[key]) > policy['axes'][axis]['readback_tolerance']:
                    raise DeviceError('PZT/TEC differs from the original study anchor')
            receipt['identity_'+capture] = instrument.driver.verify_identity(
                identity['serial_number'], identity['firmware_version'])
            receipt['model_residual_ma'][capture] = (value['current']['total_setpoint']
                - value['current']['setpoint'] - wanted*(value['piezo']['setpoint']-reference))
            return value

        before = snapshot(expected_factor, 'before')
        total, bias = before['current']['total_setpoint'], before['current']['setpoint']
        held_total = anchor['total_current_ma']
        if abs(total-held_total) > tolerance_ma:
            raise DeviceError('Total current differs from the original study anchor')
        if factor == expected_factor:
            receipt.update(intermediate=deepcopy(before), after=deepcopy(before),
                phase='complete', effect=dict(behavior='unchanged', offset_delta_ma=0.,
                                              total_delta_ma=0., response_observed=False))
            return receipt
        current_tolerance = policy['axes']['current']['readback_tolerance']
        margin = (2*tolerance_ma + 2*current_tolerance
                  + abs(factor)*policy['axes']['piezo']['readback_tolerance'])
        drive_bound = endpoints[1]-endpoints[0]
        excursion = abs(factor-expected_factor)*drive_bound + margin
        admitted = dict(drive_bound_v=drive_bound, padding_ma=margin,
            total_transition_ma=[total-excursion, total+excursion],
            bias_transition_ma=[bias-excursion, bias+excursion],
            held_total_ma=[held_total-tolerance_ma, held_total+tolerance_ma])
        for key in ('total_transition_ma', 'bias_transition_ma', 'held_total_ma'):
            for value in admitted[key]:
                bounded('current', value)
        receipt['admitted'] = admitted
        instrument.audit(dict(event='ff_adjust_preflight', channel=channel, receipt=deepcopy(receipt)))
        try:
            receipt.update(phase='factor_write', physical_changed=None, outputs_uncertain=True)
            instrument._write(channel, lease, instrument.driver.path(channel, 'cc:feedforward-factor'), factor)
            receipt['phase'] = 'intermediate_readback'
            intermediate = snapshot(factor, 'intermediate')['current']
            inside(intermediate['total_setpoint'], admitted['total_transition_ma'])
            inside(intermediate['setpoint'], admitted['bias_transition_ma'])
            held = abs(intermediate['total_setpoint']-held_total) <= tolerance_ma
            fixed = abs(intermediate['setpoint']-bias) <= current_tolerance
            if not (held or fixed):
                raise DeviceError('Unknown coefficient-write response; no automatic compensation')
            # Current-set is documented to adjust current-offset. Bound this
            # particular observed fixed-bias response before the conditional write.
            predicted_bias = intermediate['setpoint'] + held_total-intermediate['total_setpoint']
            if not held:
                compensation_bounds = [predicted_bias-2*current_tolerance,
                                       predicted_bias+2*current_tolerance]
                receipt['compensated_bias_ma'] = compensation_bounds
                for value in compensation_bounds:
                    bounded('current', value)
                    inside(value, admitted['bias_transition_ma'])
                receipt['phase'] = 'total_current_compensation'
                instrument._write(channel, lease, instrument.driver.path(channel, 'cc:current-set'),
                                  held_total, current_tolerance)
                receipt['compensation_written'] = True
            receipt['phase'] = 'final_readback'
            after = snapshot(factor, 'after')['current']
            inside(after['setpoint'], admitted['bias_transition_ma'])
            if abs(after['total_setpoint']-held_total) > tolerance_ma:
                raise DeviceError('Original total-current anchor was not restored')
            expected_bias = intermediate['setpoint'] if held else predicted_bias
            if abs(after['setpoint']-expected_bias) > 2*current_tolerance:
                raise DeviceError('Current-offset response changed after verification')
            behavior = ('no_observable_effect' if held and fixed else
                        'total_preserved' if held else 'fixed_bias_compensated')
            policy['ff_factor_ma_per_v'] = factor
            receipt.update(phase='complete', physical_changed=True, outputs_uncertain=False,
                revision_after=digest(policy), policy_adopted=True,
                effect=dict(behavior=behavior, response_observed=not (held and fixed),
                    offset_delta_ma=after['setpoint']-bias,
                    total_delta_ma=after['total_setpoint']-total))
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
