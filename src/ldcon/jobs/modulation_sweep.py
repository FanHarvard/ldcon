"""External modulation cards: broker leases, buffered ARB and verified restoration."""
import hashlib
import json
import math
import re

from ldcon.drivers.frequency_sweep import compile_sweep
from ldcon.drivers.siglent import AWGChannel, number
from ldcon.drivers.staircase import validate_channels
from ldcon.core.instrument_client import BrokerTransport, RemoteCLD1015

PROTOCOL = "ldcon.triggered-staircase/v1"
DEFAULTS = dict(full_data_log_ms=None, shape="triangle", levels_hz=[-1e6, 1e6], dwell_s=.1,
                sample_rate_hz=20000., duration_s=60., shot_sweep=None,
                sample_interval_s=.1, max_sample_age_s=1.,
                edge_s=.01, ready=None, ff_gain_scale=1., recentre_before_ff=True,   # native FF route: ramp edge, READY criteria, per-run DAC FF gain
                trigger="auto",   # locked routes without a shot: auto (self-fire once READY) | software | continuous | external
                ldc_ff_v_per_hz=0., pzt_ff_gain_scale=1., lead_s=0., tail_s=0.,
                calibrate_only=False)   # ECDL rig route: run only the owner CH2/ADC calibration (no sweep)   # ECDL rig route: LDC feed-forward study, PZT FF gain, record lead/tail


def parameters(card, overrides):
    if not isinstance(card["parameters"], dict) or not isinstance(overrides, dict):
        raise ValueError("Sweep parameters must be mappings")
    if (set(card["parameters"]) | set(overrides)) - DEFAULTS.keys():
        raise ValueError("Unknown sweep parameter")
    p = {**DEFAULTS, **card["parameters"], **overrides}
    if p["full_data_log_ms"] is not None and (type(p["full_data_log_ms"]) is not int or not 0 <= p["full_data_log_ms"] <= 100):
        raise ValueError("full_data_log_ms must be an integer 0..100 (ms) or null")
    if p["shape"] not in ("triangle", "staircase"):
        raise ValueError("Sweep shape must be triangle or staircase")
    for key in ("dwell_s", "sample_rate_hz", "duration_s", "sample_interval_s", "max_sample_age_s"):
        if type(p[key]) not in (int, float) or not math.isfinite(p[key]) or p[key] <= 0:
            raise ValueError(key + " must be positive and finite")
    if p["duration_s"] > 86400:
        raise ValueError("Sweep lifetime must be <=24 hours")
    levels = p["levels_hz"]
    if (not isinstance(levels, list) or not 0 <= len(levels) <= 10000
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in levels)):
        raise ValueError("Invalid frequency shifts")
    if p["shape"] == "triangle" and (len(levels) != 2 or levels[0] == levels[1]):
        raise ValueError("Triangle needs two distinct frequency shifts")
    if p["shot_sweep"] is not None:
        validate_request(p["shot_sweep"])
    if type(p["edge_s"]) not in (int, float) or not math.isfinite(p["edge_s"]) or p["edge_s"] <= 0 or p["edge_s"] > p["dwell_s"]:
        raise ValueError("edge_s must be positive and fit inside dwell_s")
    if p["ready"] is not None:
        from ldcon.jobs.native_ff_proxy import ready_criteria
        ready_criteria(p["ready"])  # Validate; unspecified criteria inherit the active lock.
    scale = p["ff_gain_scale"]
    if type(scale) not in (int, float) or not math.isfinite(scale) or not .5 <= scale <= 2.:
        raise ValueError("ff_gain_scale must be within [0.5, 2]")
    if type(p["recentre_before_ff"]) is not bool:
        raise ValueError("recentre_before_ff must be boolean")
    if p["trigger"] not in ("auto", "software", "continuous", "external"):
        raise ValueError("trigger must be auto, software, continuous or external")
    for key in ("ldc_ff_v_per_hz", "pzt_ff_gain_scale", "lead_s", "tail_s"):
        if type(p[key]) not in (int, float) or not math.isfinite(p[key]):
            raise ValueError(key + " must be finite")
    if type(p["calibrate_only"]) is not bool:
        raise ValueError("calibrate_only must be boolean")
    if not .5 <= p["pzt_ff_gain_scale"] <= 2. or p["lead_s"] < 0 or p["tail_s"] < 0:
        raise ValueError("pzt_ff_gain_scale must be within [0.5, 2]; lead_s/tail_s non-negative")
    return p


# Explicit shot trigger modes (no implicit fallback between them): "software" = the host fires the armed burst
# through POST /api/jobs/{job}/sweep-trigger; "external" (alias "external_rising") = the rear trigger input.
TRIGGER_MODES = ("software", "external", "external_rising")


def validate_request(request):
    required = {"schema", "request_id", "plan_sha256", "plan", "allow_simulated"}
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("Invalid shot request")
    if (request["schema"] != PROTOCOL or type(request["allow_simulated"]) is not bool
            or not re.fullmatch(r"[0-9a-f]{32}", str(request["request_id"]))):
        raise ValueError("Invalid shot protocol/identity")
    plan = request["plan"]
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False).encode()).hexdigest()
    if digest != request["plan_sha256"]:
        raise ValueError("Shot plan hash mismatch")
    if (plan["schema"] != PROTOCOL or plan["trigger_mode"] not in TRIGGER_MODES
            or plan["trigger_policy"] != "one_edge_starts_entire_record"
            or plan["axis"] not in ("current_modulation", "servo_modulation")
            or plan["reference"] != "initial_offset" or plan["unit"] not in ("V", "Hz")):
        raise ValueError("Unsupported shot plan semantics")
    shape = plan.get("shape", "staircase")
    # v1.5.1 (human 2026-10-23 ~05:10Z): 'arbitrary' = an explicit sample record (levels at dwell_s = 1/sample rate),
    # validated like a staircase record; no level analysis downstream (raw data only).
    if shape not in ("triangle", "staircase", "arbitrary"):
        raise ValueError("Invalid shot waveform")
    for key in ("dwell_s", "duration_s", "lead_s", "tail_s"):
        v = plan[key]
        if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
            raise ValueError("Invalid shot duration")
    levels = plan["levels"]
    if (not isinstance(levels, list) or not 0 <= len(levels) <= 10000
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in levels)):
        raise ValueError("Invalid shot levels")
    duration = plan["lead_s"] + len(levels)*plan["dwell_s"] + plan["tail_s"]
    if duration <= 0 or not math.isclose(duration, plan["duration_s"], rel_tol=0, abs_tol=1e-10):
        raise ValueError("Shot duration does not match record")
    if shape in ("staircase", "arbitrary"):
        settle, acquire = plan["settle_s"], plan["acquire_s"]
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (settle, acquire)):
            raise ValueError("Invalid acquisition window")
        if settle < 0 or acquire <= 0 or settle+acquire > plan["dwell_s"] + 1e-12:
            raise ValueError("Acquisition window exceeds dwell")
    return plan


def configuration(laser, role):
    binding = laser["interfaces"][role]
    device = laser["devices"][binding["device"]]
    config = {**device["connection"], **device["absolute_conditions"], "role": role}
    # Calibration and electrical bounds belong to the laser, never the deck.
    settings = device.get("control_settings", {})
    if set(settings)-{"controller_device", "frequency_hz_per_v", "reference", "load", "idle_v"}:
        raise ValueError("Unknown modulation control setting; safety limits belong to absolute_conditions")
    config.update(settings)
    guard_id = config.pop("controller_device")
    guard = laser["devices"][guard_id]
    channel = validate_channels([config])[0]
    for key in ("max_step_v", "max_slew_v_per_s"):
        value = config.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Approved " + key + " is required")
        channel[key] = value
    if not laser["allow_output"] or not channel.get("broker"):
        raise ValueError("Sweep needs output permission and an owning AWG broker")
    if channel.get("frequency_hz_per_v") is None:
        raise ValueError("Signed frequency_hz_per_v calibration is unset")
    expected = "ecdl.rig.owner" if role == "servo_modulation" else "thorlabs.cld1015.broker"
    if guard["type"] != expected:
        raise ValueError("Sweep controller owner does not match the modulation input")
    return channel, guard


class SweepPort:
    """Retain all leases on uncertain programming or restoration; never retry writes."""
    def __init__(self, laser, role, *, transport_factory=BrokerTransport, guard_factory=None):
        self.config, self.guard_device = configuration(laser, role)
        self.factory, self.guard_factory = transport_factory, guard_factory
        self.role = role
        self.channel = self.guard = self.before = None
        self.uncertain_output = False
        self.changed = False
        self.simulated = False

    def prepare(self, plan, *, triggered, journal=lambda *args: None):
        if self.channel is not None:
            raise RuntimeError("Previous sweep still owns its instruments")
        if self.guard_factory:
            self.guard = self.guard_factory(self.guard_device)
        elif self.role == "servo_modulation":
            from ldcon.jobs.ecdl_devices import OwnerSession
            from ldcon.core.interface_schema import digest
            self.guard = OwnerSession(self.guard_device)
            result = self.guard.rpc("claim", revision=digest(self.guard_device),
                                    owner="ldcon external servo modulation")
            self.guard.lease = result["lease"]
        else:
            self.guard = RemoteCLD1015({"timeout_s": 10., **self.guard_device["connection"]})
            self.guard.connect()
        try:
            io = self.factory(self.config, owner="ldcon frequency sweep")
            self.channel = AWGChannel(io, self.config)
            self.simulated = bool(getattr(io, "simulated", False) or getattr(self.guard, "simulated", False))
            self.before = self.channel.snapshot()
            old = self.before["wave"]
            old_amp = 0. if old["WVTP"] == "DC" else number(old["AMP"])
            old_offset = number(old["OFST"])
            if (old_amp < 0 or old_amp > self.config["max_vpp"]
                    or old_offset-old_amp/2 < self.config["min_v"]
                    or old_offset+old_amp/2 > self.config["max_v"]):
                raise ValueError("Original waveform is outside the approved restoration envelope")
            wave = plan.waves[self.role]
            import numpy as np
            samples = wave["codes"].astype(float)/32767*wave["amplitude_vpp"]/2+wave["offset_v"]
            # Include first/last-to-baseline and the repeat boundary.
            delta = max(float(np.max(np.abs(np.diff(np.r_[0., samples, 0.])))),
                        abs(float(samples[-1]-samples[0])),
                        abs(float(samples[0]))+old_amp/2, abs(float(samples[-1]))+old_amp/2)
            if (delta > self.config["max_step_v"]
                    or delta*plan.metadata["sample_rate_hz"] > self.config["max_slew_v_per_s"]):
                raise ValueError("Waveform including repeat/idle boundary exceeds step/slew limits")
            resolved = self.channel.resolve(wave, self.before)
            original = dict(offset_v=old_offset, amplitude_vpp=old_amp)
            if self.role == "servo_modulation":
                self.guard.call("validate_external_sweep", channel=self.config, resolved=original)
                self.guard.call("validate_external_sweep", channel=self.config, resolved=resolved)
            else:
                if self.guard.describe().get("current_writes_enabled") is not True:
                    raise ValueError("Controller owner has not enabled current output writes")
                self.guard.validate_sweep([self.config], {}, [original])
                self.guard.validate_sweep([self.config], plan.waves, [resolved])
            journal("sweep_snapshot", dict(before=self.before, resolved=resolved,
                                          metadata=plan.metadata))
            self.uncertain_output = True
            name = self.channel.upload(wave, plan.metadata, resolved, "a")
            self.changed = True
            if triggered:
                self.channel.arm(name, plan.metadata, resolved)
            else:
                self.channel.continuous(name, plan.metadata, resolved)
            self.uncertain_output = False
            return resolved
        except BaseException:
            # Unknown writes are not automatically replayed or restored.
            # Successful arm/start is the only path to automatic restoration.
            if not self.uncertain_output:
                self.close()
            raise

    def close(self):
        if self.uncertain_output:
            return
        if self.changed:
            self.uncertain_output = True
            self.channel.restore(self.before)
            self.uncertain_output = False
            self.changed = False
        if self.channel is not None:
            self.channel.io.close()
            self.channel = None
        if self.guard is not None:
            self.guard.close()
            self.guard = None

    def review_details(self):
        return dict(message="Uncertain AWG write/readback; controller and AWG leases retained.",
                    awg_lease=getattr(getattr(self.channel, "io", None), "lease", None),
                    controller_lease=getattr(self.guard, "lease", None),
                    before=self.before)


def run_sweep(port, p, *, clock, sleep, cancel, emit, publish, save_plan=lambda plan: None):
    request = p["shot_sweep"]
    plan_spec = validate_request(request) if request else None
    if plan_spec and plan_spec["axis"] != port.role:
        raise ValueError("Shot axis does not match the card")
    plan = compile_sweep([port.config], shape=plan_spec.get("shape", "staircase") if request else p["shape"],
        levels=plan_spec["levels"] if request else p["levels_hz"],
        dwell_s=plan_spec["dwell_s"] if request else p["dwell_s"],
        sample_rate_hz=p["sample_rate_hz"], unit=plan_spec["unit"] if request else "Hz",
        lead_s=plan_spec["lead_s"] if request else 0., tail_s=plan_spec["tail_s"] if request else 0.)
    if request and p["duration_s"] <= plan.metadata["duration_s"]:
        raise ValueError("Card lifetime must exceed shot duration including preparation")
    save_plan(plan)
    echo = ({key: request[key] for key in ("schema", "request_id", "plan_sha256")} if request else {})
    def report(state, restored=False):
        publish(dict(echo, state=state, armed=state == "ready", restored=restored,
                     simulated=port.simulated, trigger_mode="external_rising" if request else "continuous"))
    try:
        if cancel():
            return {"status": "cancelled"}
        resolved = port.prepare(plan, triggered=bool(request), journal=emit)
        if request and port.simulated and not request["allow_simulated"]:
            raise ValueError("Physical shot refuses a simulated instrument")
        emit("sweep_prepared", dict(metadata=plan.metadata, resolved=resolved, before=port.before))
        report("ready" if request else "playing")
        started = clock()
        while not cancel() and clock()-started < p["duration_s"]:
            sleep(.1)
        status = "cancelled" if cancel() else "succeeded"
        if request and status == "succeeded":
            raise TimeoutError("Buffered sweep lifetime expired before shot completion/stop")
        return dict(status=status, waveform=plan.metadata, simulated=port.simulated)
    finally:
        port.close()
        report("review_required" if port.uncertain_output else "restored",
               restored=not port.uncertain_output)
