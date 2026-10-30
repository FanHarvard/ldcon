"""Owner-side qualification of an already commissioned external servo input."""
import math

from ldcon.core.contracts import DeviceError


def validate_servo_sweep(owner, request):
    if owner.steady.armed or not owner.laser["allow_output"]:
        raise DeviceError("Hold the native lock before claiming external modulation")
    state = owner.hw().snapshot()
    owner.validate_state(state, owner.policies())
    owner.observation()
    cfg = state["servo_config"]
    setup = owner.device["control_settings"].get("external_modulation", {})
    # These are measured/commissioned device settings, not guessed ADC scaling.
    expected = setup.get("servo_config", {})
    required = {"baseline_control", "multiplier_control", "control_offset",
                "multiplier_target", "pid_multiplier", "software_modulation_enable"}
    if not required <= expected.keys() or any(cfg.get(k) != v for k, v in expected.items()):
        raise DeviceError("Servo external modulation configuration is not commissioned/matching")
    if (any(expected[k] != 0 for k in ("multiplier_target", "pid_multiplier", "software_modulation_enable"))
            or expected["multiplier_control"] == 0):
        raise DeviceError("External sweep requires external control only; feedback must be held")
    channel = request["channel"]
    wiring = setup["awg"]
    if any(channel.get(k) != wiring.get(k) for k in ("resource", "channel", "profile", "broker")):
        raise DeviceError("Sweep AWG does not match commissioned servo wiring")
    actual = request["resolved"]
    lo, hi = actual["offset_v"]-actual["amplitude_vpp"]/2, actual["offset_v"]+actual["amplitude_vpp"]/2
    vmin, vmax = owner.device["absolute_conditions"]["external_modulation"]["bounds_v"]
    gain, intercept = float(setup["dac_count_per_v"]), float(setup["dac_at_zero_v"])
    if not all(math.isfinite(v) for v in (lo, hi, gain, intercept, vmin, vmax)) or gain == 0:
        raise DeviceError("Invalid external input calibration")
    if not vmin <= lo <= hi <= vmax:
        raise DeviceError("External modulation exceeds approved input envelope")
    policy = owner.policies()["piezo"]
    relative = owner.device["absolute_conditions"]["axes"]["piezo"]["activation_range"]
    low = max(policy.low, owner.activation["piezo"]+relative[0])
    high = min(policy.high, owner.activation["piezo"]+relative[1])
    for voltage in (lo, hi):
        if not low <= intercept+gain*voltage <= high:
            raise DeviceError("External modulation exceeds absolute PZT limits")
    owner.audit("external_sweep_validated", voltage_bounds=[lo, hi], calibration=setup)
    return {"qualified": True}
