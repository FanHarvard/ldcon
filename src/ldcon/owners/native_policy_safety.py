"""Pure proofs for actual native outputs, separate from requested PI settings.

No hardware or durable writes. Host move budgets do not cap autonomous PI/FF.
"""
import copy
import math

from ldcon.core.instrument_client import BrokerError


def running_gain_policy(policy, config):
    """Use actual firmware P/I and raw-I bounds until a verified prepare/ramp."""
    from ldcon.owners.esp32_native_owner import number
    result = copy.deepcopy(policy)
    if number(config.get('pid_kd'), 'actual derivative gain') != 0:
        raise BrokerError('configuration', 'Actual native derivative gain is not supported')
    n = result['native']
    n['kp_count_per_nm'] = number(config.get('pid_kp'), 'actual proportional gain')
    n['ki_count_per_nm_s'] = number(config.get('pid_ki'), 'actual integral gain')
    raw = max(abs(number(config.get(key), key)) for key in ('pid_integral_min', 'pid_integral_max'))
    n['integral_limit_count'] = raw * abs(n['ki_count_per_nm_s'])
    return result


def gain_state(owner, sample=None):
    """Report desired, next-prepare and actual settings without claiming a write."""
    from ldcon.owners.native_current_tuning import effective_policy
    cfg = (sample or {}).get('servo_config') or getattr(owner, 'expected_config', None)
    if not cfg:
        cfg = ((getattr(owner, 'state', {}) or {}).get('sample') or {}).get('servo_config')
    requested = {k: owner.policy['native'][k] for k in (
        'kp_count_per_nm', 'ki_count_per_nm_s', 'integral_limit_count')}
    next_policy = effective_policy(owner.policy, getattr(owner, 'saved_pid', None))
    next_prepare = {k: next_policy['native'][k] for k in requested}
    actual = None
    if cfg and all(k in cfg for k in ('pid_kp', 'pid_ki', 'pid_kd', 'pid_integral_min', 'pid_integral_max')):
        n = running_gain_policy(owner.policy, cfg)['native']
        actual = {k: n[k] for k in requested}
    pending = actual is None or any(not math.isclose(actual[k], next_prepare[k], rel_tol=1e-7, abs_tol=1e-12)
                                    for k in next_prepare)
    return dict(requested=requested, next_prepare=next_prepare, actual=actual,
                gains_pending=pending, hardware_gains_applied=not pending)


def policy_layers(owner):
    """Expose distinct laser authorization and immutable saved physical cap."""
    from ldcon.owners.esp32_native_owner import current_for_code
    from ldcon.owners.native_modulation_limits import report
    policy = owner.policy
    low, high = policy['current_bounds_a']
    anchor = getattr(owner, 'activation_a', None)
    if anchor is not None:
        low = max(low, anchor + policy['activation_range_a'][0])
        high = min(high, anchor + policy['activation_range_a'][1])
    limits = getattr(owner, 'dac_limits', None)
    physical = None
    if limits:
        physical = {key: limits[key] for key in ('min_code', 'max_code')}
        a, b = sorted(current_for_code(policy, physical[key]) for key in ('min_code', 'max_code'))
        low, high = max(low, a), min(high, b)
    return dict(laser_current_bounds_a=list(policy['current_bounds_a']),
                activation_range_a=list(policy['activation_range_a']),
                physical_dac_limits=physical, nominal_current_intersection_a=[low, high],
                **report(policy),
                autonomous_firmware_step_slew_limited=False,
                firmware_caps_changed_by_policy=False,
                enforcement='All admitted reachable trajectories satisfy laser ranges and physical caps; measured-anchor uncertainty is checked separately. The operating policy does not reprogram firmware caps.')


def codes_proof(policy, codes, **extra):
    from ldcon.owners.esp32_native_owner import current_bounds_for_codes, number
    lo, hi = [number(value, 'reachable DAC code') for value in codes]
    if not 0 <= lo <= hi <= 65535:
        raise BrokerError('range', 'Reachable native output leaves physical DAC rails')
    codes = [math.floor(lo), math.ceil(hi)]
    return dict(code_bounds=codes, current_bounds_a=current_bounds_for_codes(policy, codes), **extra)


def actual_envelope(policy, config, *, offset=None):
    """Bound actual PI, modulation and raw-I, including held ADC and unwind to zero."""
    from ldcon.owners.esp32_native_owner import number
    value = lambda key: number(config.get(key), 'actual ' + key)
    center = value('control_offset') if offset is None else number(offset, 'candidate offset')
    adc = [value('mod_valid_min'), value('mod_valid_max')]
    if not 0 <= adc[0] <= adc[1] <= 65535:
        raise BrokerError('configuration', 'Actual ADC validity window is invalid')
    ff = [value('multiplier_control') * (x - value('baseline_control')) for x in adc] + [0.]
    correction = 0.
    if value('pid_multiplier') != 0:
        if value('pid_multiplier') != 1 or value('pid_kd') != 0:
            raise BrokerError('configuration', 'Actual native PI mode is unsupported')
        targets = [value('target') + value('multiplier_target') * (x - value('baseline_target')) for x in adc]
        targets.append(value('target'))
        systems = [value('system_min'), value('system_max')]
        if min(targets + systems) <= 0 or systems[0] > systems[1]:
            raise BrokerError('configuration', 'Actual accepted wavelength interval is invalid')
        error = max(abs(x - y) for x in systems for y in targets)
        raw = max(abs(value('pid_integral_min')), abs(value('pid_integral_max')))
        correction = abs(value('pid_kp')) * error + abs(value('pid_ki')) * raw
    return codes_proof(policy, [center + min(ff) - correction, center + max(ff) + correction],
                       correction_count=correction, source='actual_firmware_configuration')


def check_envelope(owner, policy, proof, sample=None):
    """Enforce laser bounds, measured-anchor uncertainty and physical saved caps."""
    from ldcon.owners.esp32_native_owner import current_for_code
    bounds = proof['current_bounds_a']
    absolute = policy['current_bounds_a']
    if not absolute[0] <= bounds[0] <= bounds[1] <= absolute[1]:
        raise BrokerError('range', 'Reachable native output exceeds laser absolute current range')
    activation = getattr(owner, 'activation_a', None)
    if activation is not None:
        low, high = policy['activation_range_a']
        if not activation + low <= bounds[0] <= bounds[1] <= activation + high:
            raise BrokerError('range', 'Reachable native output exceeds activation-relative current range')
        measured_anchor = getattr(owner, 'activation_measured_a', None)
        if measured_anchor is None or getattr(owner, 'activation_code', None) is None:
            raise BrokerError('configuration', 'Measured activation anchor is unavailable')
        c = policy['calibration']
        nominal = current_for_code(policy, owner.activation_code)
        values = [measured_anchor + (current_for_code(policy, code) - nominal) * factor
                  for code in proof['code_bounds']
                  for factor in (1 - c['gain_relative_uncertainty'], 1 + c['gain_relative_uncertainty'])]
        measured = [min(values), max(values)]
        if not absolute[0] <= measured[0] <= measured[1] <= absolute[1]:
            raise BrokerError('range', 'Reachable native output exceeds measured-anchor absolute current range')
        if not measured_anchor + low <= measured[0] <= measured[1] <= measured_anchor + high:
            raise BrokerError('range', 'Reachable native output exceeds measured-anchor relative current range')
        proof['measured_anchor_current_bounds_a'] = measured
    limits = (sample or {}).get('dac_limits') or getattr(owner, 'dac_limits', None)
    capabilities = ((sample or {}).get('servo_status') or {}).get('capabilities') or []
    if 'dac_limits_http_v1' in capabilities and not limits:
        raise BrokerError('configuration', 'Advertised firmware DAC limits are unavailable')
    if limits:
        lo, hi = proof['code_bounds']
        if not limits['min_code'] <= lo <= hi <= limits['max_code']:
            raise BrokerError('range', 'Reachable native output exceeds saved firmware DAC limits')
        proof['firmware_dac_limits'] = {k: limits[k] for k in ('min_code', 'max_code')}
    return proof


def reachable_proofs(owner, policy, sample):
    """Validate live output and every already-admitted FF/tuning/ramp endpoint."""
    from ldcon.owners.esp32_native_owner import C_NM, envelope
    cfg = sample['servo_config']
    proofs = [actual_envelope(policy, cfg), codes_proof(policy, [sample['dac']['native_code']] * 2,
                                                       source='observed_dac')]
    ff = getattr(owner, 'ff', None)
    if ff and ff.get('state') not in ('restored', 'fault'):
        stored = ff.get('proof') or {}
        if 'code_bounds' not in stored:
            raise BrokerError('configuration', 'Active FF reachable proof is unavailable')
        proofs.append(codes_proof(policy, stored['code_bounds'], source='active_ff_full_record_and_unwind'))
    tuning = getattr(owner, 'pid_tuning', None)
    if tuning:
        original = copy.deepcopy(tuning.original)
        proofs.append(actual_envelope(policy, original))
        destination = copy.deepcopy(cfg)
        destination.update({'pid_' + key: value for key, value in tuning.target.items()})
        proofs.append(actual_envelope(policy, destination))
    ramp = getattr(owner, 'retarget', None)
    if ramp and ramp.get('active'):
        proof = envelope(running_gain_policy(policy, cfg), C_NM / ramp['final_hz'], ramp['final_offset_estimate'])
        proof['source'] = 'admitted_target_ramp_endpoint'
        proofs.append(proof)
    for proof in proofs:
        check_envelope(owner, policy, proof, sample)
    return proofs


def integral_limit_transition(owner, policy, sample):
    """Prove a bound-only edit, including its worst-case raw-I clamp.

    Expansion preserves the accumulator; contraction can remove up to the full
    difference from an accumulator at either old bound. The bound setter clamps
    raw-I, while the DAC can still hold the preceding calculation; both complete
    envelopes stay authorized. Never infer raw-I from an observed DAC code.
    """
    from ldcon.owners.esp32_native_owner import channel_metadata, number
    from ldcon.owners.native_modulation_limits import budget_a
    cfg = sample['servo_config']
    ki = number(cfg.get('pid_ki'), 'actual integral gain')
    old_raw = number(cfg.get('pid_integral_max'), 'actual raw integral maximum', positive=True)
    low = number(cfg.get('pid_integral_min'), 'actual raw integral minimum')
    if (ki == 0 or cfg.get('pid_kd') != 0
            or not math.isclose(low, -old_raw, rel_tol=1e-9, abs_tol=1e-12)):
        raise BrokerError('configuration', 'Integral hot apply requires nonzero actual Ki, D=0 and symmetric raw bounds')
    raw = policy['native']['integral_limit_count'] / abs(ki)
    if not math.isfinite(raw) or raw <= 0:
        raise BrokerError('range', 'Requested raw integral bound is not finite and positive')
    values = dict(pid_integral_min=-raw, pid_integral_max=raw)
    target = dict(cfg, **values)
    proofs = []
    for document in (owner.policy, policy):
        for configuration, source in ((cfg, 'integral_limit_retained_accumulator'),
                                       (target, 'integral_limit_destination')):
            proof = actual_envelope(document, configuration)
            proof['source'] = source
            proofs.append(check_envelope(owner, document, proof, sample))
    clamp = max(0., old_raw - raw) * abs(ki) if cfg['pid_multiplier'] else 0.
    quantum = 1 << (16 - channel_metadata(policy['dac'])['bits'])
    quantized = clamp + 2 * quantum if clamp else 0.
    c = policy['calibration']
    per_code = abs(c['current_a_per_v'] * channel_metadata(policy['dac'])['voltage_step_v'] / quantum)
    per_code *= 1 + c['gain_relative_uncertainty']
    for document in (owner.policy, policy):
        budget = budget_a(document)
        if budget is not None and quantized * per_code > budget + 1e-15:
            raise BrokerError('range', 'Integral contraction exceeds the configured step/slew budget; use a bounded tuning trajectory')
    return dict(values=values, actual_ki_count_per_nm_s=ki,
                before_limit_count=old_raw * abs(ki), requested_limit_count=policy['native']['integral_limit_count'],
                maximum_clamp_count=clamp, quantized_clamp_bound_count=quantized,
                proofs=proofs, possible_accumulator_clamp=bool(clamp), resets_integrator=False)
