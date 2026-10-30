"""CLD1015 DOC-104674 Rev B, pp40-44: direct USBTMC/VISA, no DLL required."""
import math
import os
import hashlib
import threading
import time
from ldcon.drivers.cld1015_config import check
from ldcon.core.device_conditions import TemperatureConditions


class InvalidMeasurement(RuntimeError):
    """Invalid numeric evidence; the caller must still track any prior write."""


class PhysicalLock:
    """Keep the legacy resource flock while an owner replaces a read session."""
    def __init__(self, resource):
        if not resource or resource == "auto":
            raise ValueError("Physical ownership requires a pinned VISA resource")
        from ldcon.owners.migration_physical_lock import acquire_visa
        self.resource = resource
        self.handle = acquire_visa(resource)

    def check(self, resource):
        if self.handle.closed or self.resource != resource:
            raise ValueError("Physical lock does not own the requested VISA resource")

    def close(self):
        self.handle.close()


class VisaIO:
    def __init__(self, resource, timeout_s, *, physical_lock=None):
        import pyvisa
        self.rm = pyvisa.ResourceManager()
        self.inst = None
        self.process_lock = None
        try:
            if resource in (None, "auto"):
                candidates = []
                for address in self.rm.list_resources("USB?*::INSTR"):
                    try:
                        if int(address.split("::")[1], 0) == 0x1313:
                            candidates.append(address)
                    except (ValueError, IndexError):
                        continue
                if len(candidates) != 1:
                    raise ValueError(f"Expected one Thorlabs USB VISA device, found {len(candidates)}; check USB/driver or specify resource")
                resource = candidates[0]
            if physical_lock is not None:
                if not isinstance(physical_lock, PhysicalLock):
                    raise ValueError("Expected a held CLD1015 PhysicalLock")
                physical_lock.check(resource)
            elif os.name == "posix":
                self.process_lock = PhysicalLock(resource)
            self.inst = self.rm.open_resource(resource)
            self.inst.timeout = int(timeout_s * 1000)
            self.inst.read_termination = self.inst.write_termination = "\n"
            try:
                self.inst.lock_excl(timeout=int(timeout_s * 1000))
            except NotImplementedError:
                if self.process_lock is None and physical_lock is None:
                    raise
            except pyvisa.errors.VisaIOError as exc:
                if (self.process_lock is None and physical_lock is None) or exc.error_code != pyvisa.constants.StatusCode.error_nonsupported_operation:
                    raise
        except Exception:
            self.close()
            raise

    def query(self, command):
        return self.inst.query(command).strip()

    def write(self, command):
        self.inst.write(command)

    def close(self):
        if self.inst is not None:
            self.inst.close()
        self.rm.close()
        if self.process_lock is not None:
            self.process_lock.close()
            self.process_lock = None


class Controller:
    def __init__(self, config, factory=None):
        self.config, self.factory = config, factory
        self.io = None
        self.lock = threading.RLock()

    def connect(self):
        with self.lock:
            if self.io is None:
                self.io = (self.factory() if self.factory else MockIO() if self.config["mock"] else
                           VisaIO(self.config["resource"], self.config["timeout_s"]))
                try:
                    if "CLD1015" not in self.io.query("*IDN?").upper():
                        raise RuntimeError("USB instrument is not a CLD1015")
                except Exception:
                    self.close()
                    raise
            return self.read()

    def close(self):
        with self.lock:
            if self.io is not None:
                self.io.close()
                self.io = None

    def query(self, command):
        if self.io is None:
            raise RuntimeError("Connect to CLD1015 first")
        return self.io.query(command).strip()

    def command(self, command):
        self.io.write(command)
        if self.query("*OPC?") != "1":
            raise RuntimeError("CLD1015 command did not complete")
        error = self.query("SYST:ERR?")
        if int(error.split(",", 1)[0]) != 0:
            raise RuntimeError(f"CLD1015: {error}")

    def _unit(self):
        unit = self.query("UNIT:TEMP?").upper()
        if unit in ("C", "CEL", "CELSIUS"):
            return 1., 0.
        if unit in ("K", "KEL", "KELVIN"):
            return 1., -273.15
        if unit in ("F", "FAR", "FAHR", "FAHRENHEIT"):
            return 5 / 9, -160 / 9
        raise ValueError(f"Unknown CLD1015 temperature unit: {unit}")

    def read(self):
        with self.lock:
            scale, offset = self._unit()
            def number(cmd, field):
                raw = self.query(cmd)
                try:
                    value = float(raw)
                except (ValueError, TypeError, OverflowError) as exc:
                    raise InvalidMeasurement(f"Invalid CLD1015 {field} ({cmd}): {raw!r}") from exc
                if not math.isfinite(value):
                    raise InvalidMeasurement(f"Nonfinite CLD1015 {field} ({cmd}): {raw!r}")
                return value
            enabled = lambda cmd: self.query(cmd).upper() in ("1", "1.0", "ON")
            data = dict(current_a=number("SOUR1:CURR?", "current_a"), current_read_a=number("SENS3:CURR:DATA?", "current_read_a"),
                        temperature_c=number("SOUR2:TEMP:SPO?", "temperature_c") * scale + offset,
                        temperature_read_c=number("SENS2:TEMP:DATA?", "temperature_read_c") * scale + offset,
                        tec_current_a=number("SOUR2:CURR:DATA?", "tec_current_a"),
                        current_limit_a=number("SOUR1:CURR:LIM?", "current_limit_a"),
                        ld_enabled=enabled("OUTP1?"),
                        tec_enabled=enabled("OUTP2?"),
                        modulation_enabled=enabled("SOUR1:AM:STAT?"),
                        ld_mode=self.query("SOUR1:FUNC:MODE?"), tec_mode=self.query("SOUR2:FUNC:MODE?"))
            invalid = [key for key, value in data.items() if isinstance(value, float) and not math.isfinite(value)]
            if invalid:
                raise InvalidMeasurement("Nonfinite converted CLD1015 fields: " + ", ".join(invalid))
            return data

    def validate(self, request):
        if not request or set(request) - {"current_a", "temperature_c"}:
            raise ValueError("Setpoints must contain current_a and/or temperature_c only")
        state = self.read()
        if "current_a" in request:
            value = check(request["current_a"], self.config["current_limits_a"], "current")
            if state["ld_mode"].upper() not in ("CURR", "CURRENT"):
                raise ValueError("LD must already be in constant-current mode")
            if not 0 <= value <= state["current_limit_a"]:
                raise ValueError("Requested current exceeds controller current limit")
        if "temperature_c" in request:
            value = check(request["temperature_c"], self.config["temperature_limits_c"], "temperature")
            if state["tec_mode"].upper() not in ("TEMP", "TEMPERATURE"):
                raise ValueError("TEC must already be in temperature mode")
            scale, offset = self._unit()
            lo = float(self.query("SOUR2:TEMP:LIM:LOW?")) * scale + offset
            hi = float(self.query("SOUR2:TEMP:LIM:UPP?")) * scale + offset
            if not lo <= value <= hi:
                raise ValueError("Temperature exceeds controller temperature limits")
        return state

    def apply(self, request, *, ramp_s=0):
        with self.lock:
            if not self.config.get('temperature_writes_enabled',True):raise ValueError('Laser output writes are not enabled')
            if self.config.get('current_conditions') is not None and 'current_a' in request:
                from ldcon.owners.current_owner import apply_current
                return apply_current(self,request,ramp_s)
            before = self.validate(request)  # Validate the whole request before writing.
            ramp_s = float(ramp_s)
            if not math.isfinite(ramp_s) or not 0 <= ramp_s <= 3600:
                raise ValueError("ramp_s must be finite in 0..3600 s")
            policy = self.config.get('temperature_conditions')
            guard = TemperatureConditions(policy,self.config.get('control_settings')) if policy is not None and 'temperature_c' in request else None
            delta_temperature = abs(float(request.get('temperature_c', before['temperature_c']))-before['temperature_c'])
            if guard and not guard.low<=float(request['temperature_c'])<=guard.high:
                raise ValueError('Final temperature exceeds absolute device envelope')
            if guard and delta_temperature and ramp_s<=0:
                raise ValueError('Temperature device policy requires a timed ramp')
            steps = max(1, math.ceil(ramp_s / .1))
            if guard:
                steps=max(1,math.ceil(ramp_s/.5),math.ceil(max(0.,delta_temperature-1e-12)/guard.step))
                interval=self.config.get('control_settings',{}).get('temperature',{}).get('output_update_interval_s')
                if interval is not None:
                    interval=float(interval)
                    if not math.isfinite(interval) or not .1<=interval<=2:raise ValueError('Invalid device temperature update interval')
                    steps=max(1,math.ceil(max(0.,delta_temperature-1e-12)/guard.step))
                    if ramp_s+1e-9<steps*interval:raise ValueError('Temperature ramp is faster than device update interval')
            scale, offset = self._unit()
            previous_temperature=before['temperature_c'];previous_time=time.monotonic()
            for step in range(1, steps + 1):
                if ramp_s:time.sleep(ramp_s/steps)
                candidate=before['temperature_c']+(float(request.get('temperature_c',before['temperature_c']))-before['temperature_c'])*step/steps
                if guard:
                    observed=self.read()
                    state={'setpoint_c':observed['temperature_c'],'temperature_c':observed['temperature_read_c'],
                           'tec_current_a':observed['tec_current_a'],'current_a':observed['current_read_a'],'ld_enabled':observed['ld_enabled'],'tec_enabled':observed['tec_enabled']}
                    if abs(observed['temperature_c']-previous_temperature)>.002:
                        raise ValueError('External temperature setpoint change detected')
                    # No client-supplied elapsed time; use the owner process clock.
                    guard.check(state,candidate,time.monotonic()-previous_time,previous=previous_temperature)
                for key in ('current_a','temperature_c'):
                    if key in request:
                        value = before[key] + (float(request[key]) - before[key]) * step / steps
                        native = value if key == 'current_a' else (value - offset) / scale
                        command = 'SOUR1:CURR' if key == 'current_a' else 'SOUR2:TEMP:SPO'
                        self.command(f'{command} {native:.12g}')
                        if key=='temperature_c':previous_temperature=value;previous_time=time.monotonic()
            result = self.read()
            for key in request:
                tolerance = 6e-5 if key == "current_a" else .002
                if abs(result[key] - float(request[key])) > tolerance:
                    raise RuntimeError(f"CLD1015 setpoint verification failed: {key}")
            return result

    def enable(self, output, enabled):
        with self.lock:
            if output not in ("ld", "tec", "modulation") or not isinstance(enabled, bool):
                raise ValueError("Select ld/tec/modulation and a boolean state")
            state = self.read()
            if enabled:
                self.validate(dict(current_a=state["current_a"], temperature_c=state["temperature_c"]))
            if output == "tec" and not enabled and state["ld_enabled"]:
                raise ValueError("Turn LD off explicitly before turning TEC off")
            command = {"ld": "OUTP1", "tec": "OUTP2", "modulation": "SOUR1:AM:STAT"}[output]
            self.command(f"{command} {int(enabled)}")
            result=self.read()
            key={'ld':'ld_enabled','tec':'tec_enabled','modulation':'modulation_enabled'}[output]
            if result[key] is not enabled:
                raise RuntimeError('CLD1015 output enable verification failed: '+output)
            return result

    def validate_sweep(self, channels, waves, resolved):
        state = self.read()
        if not state["ld_enabled"] or not state["modulation_enabled"] or state["ld_mode"].upper() not in ("CURR", "CURRENT"):
            raise ValueError("CLD1015 sweep requires LD on, constant-current mode and modulation enabled")
        role = self.config["modulation_role"]
        for c, actual in zip(channels, resolved):
            if c["role"] != role:
                continue
            lo, hi = actual["offset_v"] - actual["amplitude_vpp"] / 2, actual["offset_v"] + actual["amplitude_vpp"] / 2
            # DOC-104674 p56: 150 mA/V +/-5%. Check the total LD current,
            # including the actual AWG baseline and controller DC setpoint.
            extremes = [state["current_a"] + v * gain for v in (lo, hi) for gain in (.1425, .1575)]
            for value in extremes:
                check(value, self.config["current_limits_a"], "current")
                if not 0 <= value <= state["current_limit_a"]:
                    raise ValueError("AWG envelope exceeds CLD1015 current limit")
            return
        raise ValueError("Embedded CLD1015 modulation_role does not match a sweep channel")


class MockIO:
    def __init__(self, unit="C"):
        self.unit, self.log, self.closed = unit, [], False
        self.state = {"SOUR1:CURR": .05, "SOUR2:TEMP:SPO": 25., "OUTP1": 1, "OUTP2": 1,
                      "SOUR1:AM:STAT": 1}

    def query(self, command):
        self.log.append(command)
        if command == "*IDN?": return "THORLABS,CLD1015,DUMMY,1"
        if command == "*OPC?": return "1"
        if command == "SYST:ERR?": return '0,"No error"'
        if command == "UNIT:TEMP?": return self.unit
        fixed = {"SOUR1:CURR:LIM?": .1, "SOUR2:TEMP:LIM:LOW?": 10.,
                 "SOUR2:TEMP:LIM:UPP?": 40., "SOUR1:FUNC:MODE?": "CURR", "SOUR2:FUNC:MODE?": "TEMP",
                 "SENS3:CURR:DATA?": self.state["SOUR1:CURR"],
                 "SENS2:TEMP:DATA?": self.state["SOUR2:TEMP:SPO"], "SOUR2:CURR:DATA?": .01}
        return str(fixed[command] if command in fixed else self.state[command[:-1]])

    def write(self, command):
        self.log.append(command)
        key, value = command.split()
        self.state[key] = float(value)

    def close(self):
        self.closed = True
