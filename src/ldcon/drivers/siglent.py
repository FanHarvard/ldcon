"""Siglent/BK named ARB protocol. Binary writes require an EOI transport."""
import copy
import math
import re


def fields(reply):
    text = str(reply).strip()
    text = re.sub(r"^C\d+:\w+\s+", "", text, flags=re.I)
    tokens = [x.strip() for x in text.split(",")]
    if "CARR" in tokens:
        tokens.remove("CARR")
    return dict(zip(tokens[::2], tokens[1::2]))


def number(value):
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)(?:HZ|VPP|V|S|DEG)?",
                         str(value).strip().upper())
    if not match or not math.isfinite(float(match[1])):
        raise ValueError(f"invalid numeric AWG reply: {value!r}")
    return float(match[1])


class VisaTransport:
    def __init__(self, resource, timeout_s, backend=None):
        if not resource.upper().endswith("::INSTR") or not resource.upper().startswith(("TCPIP", "USB")):
            raise ValueError("ARB upload needs a VISA INSTR (VXI-11/USB EOI), not a raw SOCKET")
        import pyvisa
        self.rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
        try:
            self.instrument = self.rm.open_resource(resource)
            self.instrument.timeout = int(timeout_s * 1000)
            self.instrument.read_termination = "\n"
            self.instrument.write_termination = "\n"
        except Exception:
            self.rm.close()
            raise

    def query(self, command):
        return self.instrument.query(command).strip()

    def write(self, command):
        self.instrument.write(command)

    def write_raw(self, data):
        self.instrument.write_raw(data)

    def close(self):
        try:
            self.instrument.close()
        finally:
            self.rm.close()


# Largest burst delay the instrument may report after BTWV DLAY,0 (2026-10-22 17:00:56Z sr1092 R1: the SDG2042X clamps
# DLAY,0 to its minimum, read back DLAY,5.61344e-05S; negligible against 0.1 s dwells). The BK reports 0.
MAX_BURST_DELAY_S = {'siglent_sdg2000x': 1e-4, 'siglent_sdg6000x': 1e-4, 'bk4050b': 1e-9}
# The SDG minimum burst delay scales with the record (burst) period: 56.1344 us at 0.9104 s, 385.724 us at 6.3104 s
# (2026-10-22 17:00:56Z and 18:54Z sr1092 R1/R3), ~6.11e-5 x period. For these profiles the limit is
# max(MAX_BURST_DELAY_S, BURST_DELAY_PER_PERIOD x record period), record period = 1/FRQ from the BSWV? readback.
BURST_DELAY_PER_PERIOD = {'siglent_sdg2000x': 1e-4, 'siglent_sdg6000x': 1e-4}


def burst_delay_limit(profile, wave):
    """Allowed 0..limit for the burst delay read back after BTWV DLAY,0 (the BK stays strict)."""
    limit = MAX_BURST_DELAY_S.get(profile, 1e-9)
    per = BURST_DELAY_PER_PERIOD.get(profile)
    if per:
        try:
            frequency = number(wave.get("FRQ"))
        except (TypeError, ValueError):
            frequency = math.nan
        if math.isfinite(frequency) and frequency > 0:
            limit = max(limit, per / frequency)
    return limit


def arb_name_matches(actual, requested):
    """The SDG reports a stored user ARB with its file suffix (ARWV NAME,lsst2a.bin, 17:00:56Z); the BK may too."""
    norm = lambda v: v[:-4] if isinstance(v, str) and v.lower().endswith('.bin') else v
    return actual is not None and norm(actual) == norm(requested)


class AWGStateMismatch(RuntimeError):
    """Every command was acknowledged and the readback was read: the AWG state is KNOWN, it is just not the requested
    one (e.g. 2026-10-22 16:39Z: burst STATE OFF after arming). Unlike a lost reply this is not an uncertain write, so
    the normal verified restore may run. ``raw`` carries the full BTWV?/BSWV?/ARWV? replies (also in the message), so a
    hardware mismatch is diagnosable from one event (2026-10-22 16:55Z: "EDGE=None" without the reply)."""
    def __init__(self, message, raw=None):
        self.raw = dict(raw or {})
        super().__init__(message + ('; raw ' + '; '.join(f"{k}={v!r}" for k, v in self.raw.items()) if self.raw else ''))


class AWGChannel:
    def __init__(self, transport, config):
        self.io, self.config = transport, config
        self.prefix = f"C{config['channel']}:"

    def command(self, command):
        if hasattr(self.io, "command"):
            self.io.command(self.prefix + command)
            return
        self.io.write(self.prefix + command)
        if self.io.query("*OPC?").strip() != "1":
            raise RuntimeError("AWG did not acknowledge command completion")

    def query(self, command):
        return self.io.query(self.prefix + command)

    def snapshot(self, *, allow_output_off=False):
        identity = self.io.query("*IDN?")
        profile = self.config["profile"]
        model = identity.upper().replace(" ", "")
        supported = ((profile == "bk4050b" and re.search(r"405[2-5]B", model)) or
                     (profile == "siglent_sdg2000x" and re.search(r"SDG(?:2042|2082|2122)X(?:,|$)", model)) or
                     (profile == "siglent_sdg6000x" and re.search(r"SDG60\d\dX(?:,|$)", model)))
        if not supported:
            raise RuntimeError(f"IDN {identity!r} does not match profile {profile}")
        state = {key: self.query(key + "?") for key in ("BSWV", "ARWV", "BTWV", "OUTP", "MDWV", "SWWV")}
        state["idn"] = identity
        state["wave"] = fields(state["BSWV"])
        state["burst"] = fields(state["BTWV"])
        if state["wave"].get("WVTP") not in ("DC", "SINE", "ARB"):
            raise RuntimeError("pre-shot waveform must be DC, SINE or named ARB")
        if state["burst"].get("STATE") != "OFF":
            raise RuntimeError("pre-shot burst must be OFF (continuous dither is supported)")
        if any(fields(state[key]).get("STATE") != "OFF" for key in ("MDWV", "SWWV")):
            raise RuntimeError("active modulation/sweep cannot be safely borrowed")
        output = state["OUTP"].split(" ", 1)[-1].replace(" ", "").split(",")
        if output[0] != "ON" and not (allow_output_off and output[0] == "OFF"):
            raise RuntimeError("AWG output must already be ON before a shot")
        load = dict(zip(output[1::2], output[2::2])).get("LOAD")
        if load != self.config["load"]:
            raise RuntimeError(f"AWG load {load!r} does not match configured {self.config['load']}")
        if dict(zip(output[1::2], output[2::2])).get("PLRT", "NOR") != "NOR":
            raise RuntimeError("inverted AWG output polarity is unsupported")
        state["arb"] = fields(state["ARWV"])
        if state["wave"]["WVTP"] == "ARB" and "NAME" not in state["arb"]:
            raise RuntimeError("cannot restore an ARB without its waveform name")
        if state["arb"].get("NAME", "").startswith("lsst"):
            raise RuntimeError("pre-shot waveform uses a reserved staircase slot; recover it first")
        if profile != "bk4050b":
            state["SRATE"] = self.query("SRATE?")
        return state

    def resolve(self, wave, state):
        base = number(state["wave"]["OFST"]) if self.config["reference"] == "initial_offset" else 0.0
        offset = base + wave["offset_v"]
        amplitude = wave["amplitude_vpp"]
        # Validate the entire analog envelope, including quantisation and idle.
        if (offset - amplitude / 2 < self.config["min_v"] - 1e-12 or
                offset + amplitude / 2 > self.config["max_v"] + 1e-12 or
                amplitude > self.config["max_vpp"] + 1e-12):
            raise ValueError(f"{self.config['role']}: actual pre-shot offset puts waveform outside voltage limits")
        return dict(offset_v=offset, amplitude_vpp=amplitude, base_v=base)

    def upload(self, wave, metadata, resolved, slot):
        name = f"lsst{self.config['channel']}{slot}"
        payload = wave["codes"].astype("<i2", copy=False).tobytes()
        header = (f"{self.prefix}WVDT WVNM,{name},LENGTH,{len(payload)},"
                  f"FREQ,{1 / metadata['duration_s']:.12g},"
                  f"AMPL,{resolved['amplitude_vpp']:.12g},OFST,{resolved['offset_v']:.12g},"
                  "PHASE,0,WAVEDATA,")
        if hasattr(self.io, "write_raw_and_wait"):
            self.io.write_raw_and_wait(header.encode("ascii") + payload)
        else:
            self.io.write_raw(header.encode("ascii") + payload)
            if self.io.query("*OPC?").strip() != "1":
                raise RuntimeError("ARB upload failed")
        return name

    MAX_BURST_CYCLES = 1000000

    def arm(self, name, metadata, resolved, source="EXT", cycles=1):
        """One-shot burst of the whole record. source EXT = rear trigger input (rising edge);
        source MAN = software trigger (fire() sends BTWV MTRIG). Siglent SDG2000X and BK 4050 share this syntax.
        cycles (v4.19, sweep API v1.2 continuous): one trigger plays the record exactly N times (BTWV TIME,N, read
        back), then idles at the record's last sample (= the centre). Bench check on the .38 SDG2042X (same model and
        firmware) 2026-10-23: TIME,N reads back, exactly N records play, the idle is the centre."""
        if source not in ("EXT", "MAN"):
            raise ValueError("burst trigger source must be EXT or MAN")
        if type(cycles) is not int or not 1 <= cycles <= self.MAX_BURST_CYCLES:
            raise ValueError("burst cycle count must be an integer 1..%d" % self.MAX_BURST_CYCLES)
        self.source = source
        self.cycles = cycles
        # Carrier FIRST, burst after (2026-10-22 16:39Z sr1092 incident): the SDG2042X ignores BTWV STATE,ON while the
        # carrier is DC, so the previous order (burst settings, then BTWV CARR,WVTP,ARB) left burst OFF and the record
        # playing CONTINUOUSLY. Selecting the ARB carrier, its record and its FRQ/AMP/OFST with BSWV before burst ON
        # works on both instruments (the BK "BSWV exits burst" rule only matters for BSWV written AFTER burst ON).
        # The carrier change happens here, before the owner enables FF (the proxy arms before ff_begin).
        self.command("BTWV STATE,OFF")
        self.command("BSWV WVTP,ARB")
        self.command(f"ARWV NAME,{name}")
        if self.config["profile"] != "bk4050b":
            self.command("SRATE MODE,DDS")
        self.command(f"BSWV FRQ,{1 / metadata['duration_s']:.12g},"
                     f"AMP,{resolved['amplitude_vpp']:.12g},OFST,{resolved['offset_v']:.12g}")
        self.command("BTWV STATE,ON")
        self.command(f"BTWV TRSR,{source}")
        self.command("BTWV GATE_NCYC,NCYC")
        self.command("BTWV TIME,%d" % cycles)
        self.command("BTWV STPS,0")
        self.command("BTWV DLAY,0")
        self.command("BTWV EDGE,RISE")
        self.verify(name, metadata, resolved)
        return dict(burst_delay_s=getattr(self, 'burst_delay_s', 0.))

    def fire(self):
        """Software trigger of an armed MAN burst: returns {ok, source, command, readback} (readback = BTWV? after
        the command; the instrument does not report the shot itself, the host confirms playback on its own input)."""
        if getattr(self, "source", None) != "MAN":
            raise RuntimeError("fire() needs a burst armed with the MAN trigger source")
        command = self.prefix + "BTWV MTRIG"
        self.command("BTWV MTRIG")
        readback = self.query("BTWV?")
        burst = fields(readback)
        if burst.get("STATE") != "ON" or burst.get("TRSR") != "MAN":
            raise RuntimeError("burst state changed around the software trigger: " + str(readback))
        return dict(ok=True, source="MAN", command=command, readback=str(readback).strip())

    def raw_state(self):
        """The full replies the verification looked at (for mismatch messages and journals)."""
        return {key: str(self.query(key + "?")).strip() for key in ("BTWV", "BSWV", "ARWV")}

    def verify(self, name, metadata, resolved):
        raw = self.raw_state()
        burst, wave, arb = fields(raw["BTWV"]), fields(raw["BSWV"]), fields(raw["ARWV"])
        source = getattr(self, "source", "EXT")
        wanted = {"STATE": "ON", "TRSR": source, "GATE_NCYC": "NCYC"}
        if source == "EXT":
            # EDGE is reported (and meaningful) only for an external trigger source; under MAN the SDG2000X reply has
            # no EDGE field (2026-10-22 16:55Z, sr1092 R1: "AWG arming mismatch: EDGE=None").
            wanted["EDGE"] = "RISE"
        for key, value in wanted.items():
            if burst.get(key) != value:
                raise AWGStateMismatch(f"AWG arming mismatch: {key}={burst.get(key)!r}", raw)
        for key, value, required in (("TIME", getattr(self, "cycles", 1), True), ("STPS", 0, False)):
            if key not in burst:
                if required:
                    raise AWGStateMismatch(f"AWG arming mismatch: {key} missing", raw)
                continue                      # optional in a reply; when present it must match
            try:
                ok = math.isclose(number(burst[key]), value, abs_tol=1e-9)
            except ValueError:
                ok = False
            if not ok:
                raise AWGStateMismatch(f"AWG arming mismatch: {key}={burst[key]!r}", raw)
        delay = 0.
        if "DLAY" in burst:
            # DLAY,0 is clamped to the instrument minimum (it scales with the record period on the SDG): accept
            # 0 <= DLAY <= burst_delay_limit, report it.
            try:
                delay = number(burst["DLAY"])
            except ValueError:
                delay = math.nan
            limit = burst_delay_limit(self.config["profile"], wave)
            if not 0 <= delay <= limit:
                raise AWGStateMismatch(f"AWG arming mismatch: DLAY={burst['DLAY']!r} (allowed 0..{limit:g} s)", raw)
        # Fields present in the SDG MAN reply that are informational and not checked: PRD (the burst period the
        # instrument derives), TRMD (trigger-out mode), COUNTER/COUNT (shot counter), CARR,... (the carrier, checked via
        # BSWV? below). EDGE only exists for EXT.
        self.burst_delay_s = delay
        if not arb_name_matches(arb.get("NAME"), name):
            raise AWGStateMismatch(f"AWG selected the wrong waveform: {arb.get('NAME')!r}", raw)
        if wave.get("WVTP") != "ARB":
            raise AWGStateMismatch(f"AWG carrier is not ARB: {wave.get('WVTP')!r}", raw)
        for key, value in {"FRQ": 1 / metadata["duration_s"], "AMP": resolved["amplitude_vpp"],
                           "OFST": resolved["offset_v"]}.items():
            try:
                ok = math.isclose(number(wave[key]), value, rel_tol=2e-5, abs_tol=1e-5)
            except (KeyError, ValueError):
                ok = False
            if not ok:
                raise AWGStateMismatch(f"AWG carrier readback mismatch: {key}={wave.get(key)!r}", raw)

    def continuous(self, name, metadata, resolved):
        """Select the uploaded arbitrary record and repeat using the AWG clock."""
        self.command("BTWV STATE,OFF")
        self.command("BSWV WVTP,ARB")
        self.command("ARWV NAME," + name)
        if self.config["profile"] != "bk4050b":
            self.command("SRATE MODE,DDS")
        self.command(f"BSWV FRQ,{1/metadata['duration_s']:.12g},AMP,{resolved['amplitude_vpp']:.12g},OFST,{resolved['offset_v']:.12g}")
        wave = fields(self.query("BSWV?"))
        if (fields(self.query("BTWV?")).get("STATE") != "OFF"
                or not arb_name_matches(fields(self.query("ARWV?")).get("NAME"), name)
                or wave.get("WVTP") != "ARB"):
            raise AWGStateMismatch("Continuous waveform selection was not confirmed", self.raw_state())
        for key, wanted in {"FRQ": 1/metadata["duration_s"], "AMP": resolved["amplitude_vpp"],
                            "OFST": resolved["offset_v"]}.items():
            if not math.isclose(number(wave[key]), wanted, rel_tol=2e-5, abs_tol=1e-5):
                raise AWGStateMismatch("Continuous waveform readback mismatch: " + key, self.raw_state())

    def restore(self, state):
        if state["wave"]["WVTP"] == "DC":
            return self._restore_dc(state)
        self.command("BTWV STATE,OFF")
        wave = state["wave"]
        self.command("BSWV WVTP," + wave["WVTP"])
        if wave["WVTP"] == "ARB":
            self.command("ARWV NAME," + state["arb"]["NAME"])
        if "SRATE" in state:
            self.command("SRATE " + state["SRATE"].split(" ", 1)[1])
        keys = ("OFST",) if wave["WVTP"] == "DC" else ("FRQ", "AMP", "OFST", "PHSE")
        self.command("BSWV " + ",".join(f"{key},{wave[key]}" for key in keys if key in wave))
        actual = fields(self.query("BSWV?"))
        if actual.get("WVTP") != wave["WVTP"]:
            raise RuntimeError("could not restore original waveform type")
        for key in keys:
            if key in wave and not math.isclose(number(actual[key]), number(wave[key]), rel_tol=2e-5, abs_tol=1e-5):
                raise RuntimeError(f"could not restore {key}")
        if wave["WVTP"] == "ARB" and not arb_name_matches(fields(self.query("ARWV?")).get("NAME"), state["arb"]["NAME"]):
            raise RuntimeError("could not restore dither waveform")
        if self.query("OUTP?") != state["OUTP"]:
            raise RuntimeError("output state changed during the shot")

    def _restore_dc(self, state):
        """Restore a DC rest state (2026-10-22 R1 finding on the SDG2042X CH2) through park_dc: BSWV WVTP,DC first,
        then burst OFF and the recorded OFST, then verify. It never re-selects the recorded ARWV name/index and never writes SRATE (both
        belong to ARB playback; on the SDG an SRATE/ARWV write re-selects ARB, which is how a restore left the uploaded
        staircase playing continuously). If the channel still reads an ARB after the restore, the output is switched
        OFF as the fail-safe (a continuously playing ARB is never left behind) and the restore raises for review."""
        actual = self.park_dc(state["wave"]["OFST"])
        if self.query("OUTP?") != state["OUTP"]:
            raise RuntimeError("output state changed during the shot")
        return dict(WVTP="DC", OFST=actual.get("OFST"))

    def parked_dc(self, offset):
        """True when the channel is a static DC at ``offset`` with burst OFF (read-only)."""
        wave, burst = fields(self.query("BSWV?")), fields(self.query("BTWV?"))
        return (wave.get("WVTP") == "DC" and burst.get("STATE") == "OFF"
                and math.isclose(number(wave.get("OFST", "nan")), number(offset), rel_tol=2e-5, abs_tol=1e-5))

    def park_dc(self, offset):
        """BSWV WVTP,DC FIRST, then burst OFF, then BSWV OFST,<offset>, verify. No ARWV/SRATE write.

        DC first (2026-10-22 v4.4, the owner's manual park after the 17:26:58Z R1): with an ARB carrier, burst OFF
        before the type change plays the record CONTINUOUSLY until the DC write lands; on the SDG2042X, BSWV WVTP,DC
        while burst is ON exits burst, and with burst already OFF it stops the continuous ARB at once. So at no point
        between the commands does the channel play an ARB continuously. BTWV STATE,OFF then makes the burst state
        explicit (a no-op on the SDG after the DC write). A channel that still reads a continuously playing ARB
        afterwards is switched OFF (fail-safe) and the call raises for review."""
        wave = dict(OFST=str(offset))
        self.command("BSWV WVTP,DC")
        self.command("BTWV STATE,OFF")
        self.command("BSWV OFST," + wave["OFST"])
        actual = fields(self.query("BSWV?"))
        burst = fields(self.query("BTWV?"))
        problems = []
        if actual.get("WVTP") != "DC":
            problems.append("waveform reads %r, not DC" % actual.get("WVTP"))
        elif not math.isclose(number(actual.get("OFST", "nan")), number(wave["OFST"]), rel_tol=2e-5, abs_tol=1e-5):
            problems.append("offset reads %r, recorded %r" % (actual.get("OFST"), wave["OFST"]))
        if burst.get("STATE") != "OFF":
            problems.append("burst reads %r" % burst.get("STATE"))
        if problems:
            if actual.get("WVTP") == "ARB" and burst.get("STATE") == "OFF":
                self.command("OUTP OFF")
                state_after = self.query("OUTP?").split(" ", 1)[-1].replace(" ", "").split(",")[0]
                problems.append("output switched %s to stop a continuously playing ARB (fail-safe)" % state_after)
            raise RuntimeError("could not restore the DC rest state: " + "; ".join(problems))
        return actual


class DummyTransport:
    """Stateful SCPI emulator, including the BK BSWV-exits-burst behaviour."""
    def __init__(self, config):
        self.log, self.uploads = [], {}
        self.idn = {"bk4050b": "BK PRECISION,4054B,DUMMY,0",
                    "siglent_sdg2000x": "SIGLENT,SDG2042X,DUMMY,0",
                    "siglent_sdg6000x": "SIGLENT,SDG6022X,DUMMY,0"}[config["profile"]]
        base = (config["min_v"] + config["max_v"]) / 2
        self.state = {"BSWV": {"WVTP": "ARB", "FRQ": "0.5", "AMP": "0.002", "OFST": str(base), "PHSE": "0"},
                      "ARWV": {"NAME": "original_dither"}, "BTWV": {"STATE": "OFF"},
                      "SRATE": {"MODE": "DDS"}, "MDWV": {"STATE": "OFF"}, "SWWV": {"STATE": "OFF"}}
        self.load = config["load"]
        self.original = copy.deepcopy(self.state)
        self.fail_on = None
        self.closed = False
        self.fired = []                     # MTRIG software triggers received (burst must be ON, TRSR MAN)

    def query(self, command):
        self.log.append(command)
        if command == "*IDN?":
            return self.idn
        if command == "*OPC?":
            return "1"
        prefix, key = command[:-1].split(":")
        if key == "OUTP":
            return f"{prefix}:OUTP ON,LOAD,{self.load},PLRT,NOR"
        return f"{prefix}:{key} " + ",".join(f"{k},{v}" for k, v in self.state[key].items())

    def write(self, command):
        self.log.append(command)
        if self.fail_on and self.fail_on in command:
            self.fail_on = None
            raise OSError("injected AWG write failure")
        _, rest = command.split(":", 1)
        key, value = rest.split(" ", 1)
        if key == "BTWV" and value.strip() == "MTRIG":
            if self.state["BTWV"].get("STATE") != "ON" or self.state["BTWV"].get("TRSR") != "MAN":
                raise OSError("MTRIG ignored: burst not armed with the MAN source")
            self.fired.append(len(self.log))
            return
        if key == "BSWV":
            self.state["BTWV"]["STATE"] = "OFF"
        if key == "BTWV" and value.startswith("CARR,"):
            key, value = "BSWV", value[5:]
        self.state[key].update(fields(value))

    def write_raw(self, data):
        header, payload = data.split(b"WAVEDATA,", 1)
        self.log.append(header.decode() + "<binary>")
        name = fields(header.decode().rstrip(","))["WVNM"]
        self.uploads[name] = payload

    def close(self):
        self.closed = True
